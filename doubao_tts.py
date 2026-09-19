#!/usr/bin/env python3
"""
豆包 TTS (Text-to-Speech) 逆向工程客户端
Doubao TTS Reverse Engineering Client

用法:
    python doubao_tts.py "你好，世界" -o output.mp3
    python doubao_tts.py "Hello World" --speaker en_female_sarah_conversation_bigtts -o hello.mp3
"""

import argparse
import asyncio
import hashlib
import http.client
import json
import random
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

try:
    import websockets
except ImportError:
    print("请安装 websockets: pip install websockets")
    exit(1)


@dataclass
class TTSConfig:
    """TTS 配置"""
    # 语音角色 ID（豆包前端默认 zh_female_wenroutaozi_uranus_bigtts）
    speaker: str = "zh_female_wenroutaozi_uranus_bigtts"
    # 音频格式: mp3, ogg_opus, wav, pcm
    format: str = "mp3"
    # 语速倍率, 1.0 为正常（豆包前端取值范围约 0.5 ~ 2.0）
    speech_rate: float = 1.0
    # 音调, 单位半音, 范围 -12 ~ 12, 0 为正常
    pitch: int = 0
    # Cookie (从浏览器获取)
    cookie: str = ""


@dataclass
class TTSResult:
    """TTS 结果"""
    audio_data: bytes = field(default_factory=bytes)
    sentences: list = field(default_factory=list)
    success: bool = False
    error: str = ""


# ---------------- 音色表 ----------------
# voices.json 由 fetch_voices.py 从豆包 /alice/user_voice/recommend 拉取。
# 服务端对无法识别的 speaker ID 会静默回退默认音色而不报错，所以客户端必须自己校验。
VOICES_FILE = Path(__file__).parent / "voices.json"

# 少量常用简称，方便命令行；完整列表见 voices.json
SPEAKERS = {
    "taozi": "zh_female_wenroutaozi_uranus_bigtts",       # 温柔桃子（前端默认）
    "vv": "zh_female_vv_uranus_bigtts",                   # 女声 vv
    "shuangkuai": "zh_female_shuangkuaisisi_moon_bigtts",  # 爽快
    "yangguang": "zh_male_yangguang_conversation_v4_wvae_bigtts",  # 阳光男声
    "rap": "zh_male_rap_mars_bigtts",                     # 说唱男声
    "en_female": "en_female_sarah_conversation_bigtts",
    "en_male": "en_male_adam_conversation_bigtts",
}


def load_voices() -> list[dict]:
    """读取 voices.json；文件缺失或损坏时返回空列表（调用方决定如何降级）。"""
    if not VOICES_FILE.exists():
        return []
    try:
        data = json.loads(VOICES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    voices = data.get("voices") if isinstance(data, dict) else data
    return voices if isinstance(voices, list) else []


def _build_index() -> tuple[dict[str, str], dict[str, dict]]:
    """构建 名称/别名/speaker_id -> speaker_id 的查找表，及 speaker_id -> 详情。"""
    by_id: dict[str, dict] = {}
    lookup: dict[str, str] = {}
    for v in load_voices():
        sid = v.get("speaker_id", "")
        if not sid:
            continue
        by_id[sid] = v
        lookup[sid.lower()] = sid
        name = (v.get("name") or "").strip()
        if name:
            # 中文名也可直接作为 voice 传入；重名时保留先出现的
            lookup.setdefault(name.lower(), sid)
    # 简称优先级最高，覆盖同名条目；其指向的 speaker_id 也要可解析
    for alias, sid in SPEAKERS.items():
        lookup[alias.lower()] = sid
        lookup.setdefault(sid.lower(), sid)
        by_id.setdefault(sid, {"speaker_id": sid, "name": alias, "tags": [],
                               "tab": "alias", "language": ""})
    return lookup, by_id


_VOICE_LOOKUP, _VOICE_BY_ID = _build_index()


def resolve_speaker(voice: str) -> str | None:
    """把用户输入（简称 / speaker_id / 中文名）解析为 speaker_id；无法匹配返回 None。"""
    if not voice:
        return None
    return _VOICE_LOOKUP.get(voice.strip().lower())


def voice_catalog() -> list[dict]:
    """返回全部已知音色详情（含 voices.json 与内置简称）。"""
    return sorted(_VOICE_BY_ID.values(), key=lambda v: v.get("speaker_id", ""))


# ---------------- 协议常量（逆向自豆包前端 s2-doubao-speech-sdk.js）----------------
APPKEY = "GOqQpfo1fO7slHv8"
NAMESPACE = "VoiceGenie"
WS_URL = "wss://frontier-audio-web-ws.doubao.com/api/v2/sami/voicegenie"

# request_type 枚举: ASR=3 ASR_RETRY=6 TTS=4 TTS_RETRY=7 TEXT_TTS=5
REQUEST_TYPE_TEXT_TTS = 5

# 上行事件
EV_START_TASK = "StartTask"
EV_START_SESSION = "StartSession"
EV_BIDIRECTIONAL_TTS = "BidirectionalTTS"
EV_END_TTS = "EndTTS"
# 下行事件
EV_TASK_STARTED = "TaskStarted"
EV_SESSION_STARTED = "SessionStarted"
EV_SENTENCE_START = "TTSSentenceStart"
EV_SENTENCE_END = "TTSSentenceEnd"
EV_TTS_RESPONSE = "TTSResponse"
EV_TTS_ENDED = "TTSEnded"
EV_SESSION_FINISHED = "SessionFinished"
EV_SESSION_FAILED = "SessionFailed"
EV_TASK_FAILED = "TaskFailed"


# ---------------- 极简 Protobuf 编解码（无 protobuf 依赖） ----------------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field_str(num: int, val: str) -> bytes:
    b = val.encode("utf-8")
    return _varint(num << 3 | 2) + _varint(len(b)) + b


def _encode_request(*, token="", appkey="", namespace="", version="",
                    event="", payload="", task_id="", session_id="") -> bytes:
    """data.speech.gateway.WebSocketRequest
    token=1 appkey=2 namespace=3 version=4 event=5 payload=6
    data=7(bytes) task_id=8 seq_id=9 session_id=10"""
    fields = {"token": 1, "appkey": 2, "namespace": 3, "version": 4,
              "event": 5, "payload": 6, "task_id": 8, "session_id": 10}
    vals = {"token": token, "appkey": appkey, "namespace": namespace,
            "version": version, "event": event, "payload": payload,
            "task_id": task_id, "session_id": session_id}
    return b"".join(_field_str(n, vals[k]) for k, n in fields.items() if vals[k])


def _decode_response(buf: bytes) -> dict:
    """data.speech.gateway.WebSocketResponse
    task_id=1 message_id=2 namespace=3 event=4 status_code=5 status_text=6
    payload=7 data=8(bytes) seq_id=9 session_id=10 log_id=11"""
    names = {1: "task_id", 2: "message_id", 3: "namespace", 4: "event",
             5: "status_code", 6: "status_text", 7: "payload", 8: "data",
             9: "seq_id", 10: "session_id", 11: "log_id"}
    out, i = {}, 0
    while i < len(buf):
        tag, shift = 0, 0
        while True:
            b = buf[i]
            i += 1
            tag |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        num, wt = tag >> 3, tag & 7
        if wt == 0:  # varint
            v, shift = 0, 0
            while True:
                b = buf[i]
                i += 1
                v |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            out[names.get(num, num)] = v
        elif wt == 2:  # length-delimited
            ln, shift = 0, 0
            while True:
                b = buf[i]
                i += 1
                ln |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            raw = buf[i:i + ln]
            i += ln
            key = names.get(num, num)
            out[key] = raw if key == "data" else raw.decode("utf-8", "replace")
        else:  # 不支持的 wire type
            break
    return out


class DoubaoTTS:
    """豆包 TTS 客户端（VoiceGenie 协议）"""

    def __init__(self, config: TTSConfig | None = None):
        self.config = config or TTSConfig()
        # 同一 cookie 始终映射到同一组设备 ID，避免被风控识别为大量新设备
        self._device_id = self._stable_id(self.config.cookie)
        self._web_id = self._stable_id(self.config.cookie + "_web")

    @staticmethod
    def _stable_id(seed: str) -> str:
        s = seed or str(random.randint(0, 2**64))
        h = int(hashlib.sha256(s.encode()).hexdigest()[:16], 16)
        return str(7600000000000000000 + h % 99999999999999999)

    def _build_ws_url(self) -> str:
        params = {
            "api_app_key": APPKEY, "namespace": NAMESPACE,
            "version_code": "20800", "language": "zh", "device_platform": "web",
            "pkg_type": "release_version", "pc_version": "3.37.5",
            "region": "CN", "sys_region": "CN", "samantha_web": "1",
            "use-olympus-account": "1", "doubao_device_platform": "web",
            "aid": "497858", "real_aid": "497858",
            "device_id": self._device_id, "doubao_pc_version": "3.37.5",
            "web_id": self._web_id, "tea_uuid": self._web_id,
            "web_platform": "browser", "web_tab_id": str(uuid.uuid4()),
        }
        return WS_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    def _session_payload(self) -> str:
        audio_cfg: dict = {"format": self.config.format}
        if self.config.format in ("ogg_opus", "mp3"):
            audio_cfg["bit_rate"] = 32000
        if self.config.format == "ogg_opus":
            audio_cfg["sample_rate"] = 24000
        payload = {
            "business": 1, "conversation_id": "",
            "request_type": REQUEST_TYPE_TEXT_TTS,
            "enable_text_reading": True, "interrupt_type": 0, "query_mode": 2,
            "chat": {"bot_id": "", "conversation_id": "", "question_id": "0",
                     "message_id": "", "new_conversation": True, "extra": {}},
            "tts": {"speaker": self.config.speaker, "audio_config": audio_cfg,
                    "extra": {"cache_config": {"text_type": 1, "use_cache": True},
                              "network_level": 7,
                              "post_process": {"pitch": self.config.pitch,
                                               "speech_rate": self.config.speech_rate},
                              "music_ext_for_tts": ""}},
            "extra": {"disable_markdown_filter": True,
                      "extra": json.dumps({
                          "post_process": {"pitch": self.config.pitch,
                                           "speech_rate": self.config.speech_rate},
                          "cache_config": {"text_type": 1, "use_cache": True},
                          "music_ext_for_tts": "", "network_level": 7})},
        }
        return json.dumps(payload, ensure_ascii=False)

    async def _recv(self, ws, timeout=20) -> dict:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        return _decode_response(raw if isinstance(raw, bytes) else raw.encode())

    async def synthesize(
        self,
        text: str,
        on_audio_chunk: Callable[[bytes], None] | None = None,
        on_sentence_start: Callable[[str], None] | None = None,
        on_sentence_end: Callable[[], None] | None = None,
    ) -> TTSResult:
        """合成语音。audio_data 为 config.format 指定的编码音频。"""
        result = TTSResult()
        audio_chunks = []

        headers = {
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache", "Pragma": "no-cache",
            "Origin": "https://www.doubao.com",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/153.0.0.0 Safari/537.36",
        }
        if self.config.cookie:
            headers["Cookie"] = self.config.cookie

        try:
            async with websockets.connect(
                self._build_ws_url(),
                additional_headers=headers,
                proxy=None,  # 跳过系统代理，直连 doubao WebSocket
                max_size=None,
            ) as ws:
                async def send(event, payload="", task_id=""):
                    await ws.send(_encode_request(
                        appkey=APPKEY, namespace=NAMESPACE,
                        event=event, payload=payload, task_id=task_id))

                # 1) StartTask -> TaskStarted
                await send(EV_START_TASK)
                r = await self._recv(ws)
                if r.get("event") != EV_TASK_STARTED:
                    result.error = (f"StartTask 失败: "
                                    f"{r.get('status_code')} {r.get('status_text')}")
                    print(f"[ERROR] {result.error}")
                    return result
                task_id = r.get("task_id", "")
                print(f"[INFO] 任务已开始 task_id={task_id}")

                # 2) StartSession -> SessionStarted
                await send(EV_START_SESSION, self._session_payload(), task_id)
                r = await self._recv(ws)
                if r.get("event") != EV_SESSION_STARTED:
                    result.error = (f"StartSession 失败: "
                                    f"{r.get('status_code')} {r.get('status_text')}")
                    print(f"[ERROR] {result.error}")
                    return result
                print("[INFO] 会话已开始")

                # 3) BidirectionalTTS 发送文本, EndTTS 收尾
                await send(EV_BIDIRECTIONAL_TTS,
                           json.dumps({"text": text}, ensure_ascii=False), task_id)
                await send(EV_END_TTS, task_id=task_id)

                # 4) 接收事件流直到 TTSEnded
                while True:
                    try:
                        r = await self._recv(ws, timeout=30)
                    except asyncio.TimeoutError:
                        print("[INFO] 接收超时，合成结束")
                        break

                    ev = r.get("event")
                    if r.get("data"):
                        audio_chunks.append(r["data"])
                        if on_audio_chunk:
                            on_audio_chunk(r["data"])

                    if ev == EV_SENTENCE_START:
                        print("[句子] 开始")
                    elif ev == EV_SENTENCE_END:
                        try:
                            sentence = json.loads(r.get("payload", "{}")).get("text", "")
                        except (json.JSONDecodeError, AttributeError):
                            sentence = ""
                        if sentence:
                            result.sentences.append(sentence)
                            if on_sentence_start:
                                on_sentence_start(sentence)
                            print(f"[句子] {sentence[:50]}")
                        if on_sentence_end:
                            on_sentence_end()
                    elif ev == EV_TTS_ENDED:
                        print("[INFO] TTS 完成")
                        break
                    elif ev == EV_SESSION_FINISHED:
                        print("[INFO] 会话结束")
                        break
                    elif ev in (EV_SESSION_FAILED, EV_TASK_FAILED):
                        result.error = (f"{ev}: {r.get('status_code')} "
                                        f"{r.get('status_text')}")
                        print(f"[ERROR] {result.error}")
                        break

        except Exception as e:
            result.error = str(e)
            print(f"[ERROR] 连接失败: {e}")
            return result

        result.audio_data = b"".join(audio_chunks)
        result.success = len(result.audio_data) > 0
        print(f"[INFO] 合成完成, 音频大小: {len(result.audio_data)} bytes")
        return result

    def synthesize_sync(self, text: str, **kwargs) -> TTSResult:
        """同步版本的合成方法"""
        return asyncio.run(self.synthesize(text, **kwargs))

    def set_speaker(self, speaker: str):
        """设置语音角色（支持简称 / speaker_id / 中文名）。

        未在音色表中的值会原样透传（CLI 保留灵活性）；
        需要严格校验的调用方请先用 resolve_speaker() 判断。
        """
        self.config.speaker = resolve_speaker(speaker) or speaker
        return self

    def set_speed(self, speed: float):
        """设置语速倍率 (0.5 ~ 2.0, 1.0 为正常)"""
        try:
            v = float(speed)
        except (TypeError, ValueError):
            v = 1.0
        self.config.speech_rate = max(0.5, min(2.0, v))
        return self

    def set_pitch(self, pitch: float):
        """设置音调 (-12 ~ 12 半音, 0 为正常)"""
        try:
            v = int(float(pitch))
        except (TypeError, ValueError):
            v = 0
        self.config.pitch = max(-12, min(12, v))
        return self


# ---------------- Cookie 双文件与保温 ----------------
# 两份 cookie 文件，内容始终保持同步：
#   .cookie         原始 Cookie 头字符串（用户从 DevTools 复制的格式）
#   .doubao_cookie  JSON 数组（带 expirationDate，用于判断过期）
# 续期成功后两份都会被更新，避免其中一份变成过期数据。
COOKIE_FILE = Path(__file__).parent / ".doubao_cookie"
RAW_COOKIE_FILE = Path(__file__).parent / ".cookie"

# 登录态核心 cookie：这几个过期就需要重新登录。
CORE_COOKIES = (
    "sessionid_ss", "uid_tt_ss", "sid_ucp_v1", "ssid_ucp_v1",
    "session_tlb_tag", "passport_auth_status_ss",
)

# 字节 passport 的心跳续期端点。调一次就把核心 cookie 重新计 ~29.9 天（滚动续期）。
RENEW_HOST = "www.doubao.com"
RENEW_PATH = "/passport/token/beat/v2/?aid=497858"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


def _read_json_items() -> list[dict] | None:
    """读取 .doubao_cookie 的 JSON 数组；不可用时返回 None。"""
    if not COOKIE_FILE.exists():
        return None
    try:
        items = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return items if isinstance(items, list) else None


def _read_raw_cookie() -> str:
    """读取 .cookie 的原始字符串；缺失或空则返回空串。"""
    if not RAW_COOKIE_FILE.exists():
        return ""
    try:
        return RAW_COOKIE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _items_to_header(items: list[dict]) -> str:
    """JSON 条目 -> Cookie 请求头字符串。"""
    return "; ".join(f"{c['name']}={c['value']}" for c in items
                     if isinstance(c, dict) and "name" in c and "value" in c)


def _header_to_items(header: str, base: list[dict] | None = None) -> list[dict]:
    """Cookie 头字符串 -> JSON 条目。base 提供已有条目时沿其 expirationDate 等元数据。"""
    known = {c["name"]: c for c in (base or [])
             if isinstance(c, dict) and "name" in c}
    out: list[dict] = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        entry = dict(known.get(name) or {
            "domain": ".doubao.com", "hostOnly": False, "httpOnly": True,
            "name": name, "path": "/", "sameSite": "no_restriction",
            "secure": True, "session": False, "storeId": None,
        })
        entry["name"] = name
        entry["value"] = value
        out.append(entry)
    return out


def _atomic_write(path: Path, text: str) -> None:
    """写文件：优先原子替换，不行就直写。

    原子写避免中途失败把登录态写坏；但 Docker 单文件 bind mount 的 inode
    被挂载点固定，rename 覆盖会报 EBUSY（跨挂载点还会报 EXDEV），此时退回直写。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.replace(path)
    except OSError:
        # ponytail: 失去原子性（极小窗口内崩溃可能写半截），容器挂载场景无更好选择。
        try:
            path.write_text(text, encoding="utf-8")
        finally:
            tmp.unlink(missing_ok=True)


def _persist_both(items: list[dict]) -> None:
    """把同一份 cookie 同时写入 JSON 和原始字符串两个文件。"""
    _atomic_write(COOKIE_FILE,
                  json.dumps(items, ensure_ascii=False, indent=4) + "\n")
    _atomic_write(RAW_COOKIE_FILE, _items_to_header(items) + "\n")


def _expiry_from_sid_guard(items: list[dict]) -> float | None:
    """从 sid_guard 推算登录态到期时间戳（<sessionid>|<签发时间>|<TTL 秒>|...）。"""
    for c in items:
        if not isinstance(c, dict) or c.get("name") != "sid_guard":
            continue
        parts = urllib.parse.unquote(str(c.get("value", ""))).split("|")
        if len(parts) < 3:
            continue
        try:
            return int(parts[1]) + int(parts[2])
        except ValueError:
            continue
    return None


def cookie_expiry_days(items: list[dict] | None = None) -> float | None:
    """返回核心登录态最早还有多少天过期；无法判断时返回 None。

    优先用 expirationDate；纯文本转来的没有该字段，则从 sid_guard 推算。
    """
    if items is None:
        items = _read_json_items()
    if not items:
        return None
    now = time.time()
    days = [
        (c["expirationDate"] - now) / 86400
        for c in items
        if isinstance(c, dict) and c.get("name") in CORE_COOKIES
        and isinstance(c.get("expirationDate"), (int, float))
    ]
    if days:
        return min(days)
    exp = _expiry_from_sid_guard(items)
    return (exp - now) / 86400 if exp else None


def load_cookie_from_file() -> str:
    """加载 cookie 并返回请求头字符串。优先 JSON（带过期信息），回退纯文本。"""
    items = _read_json_items()
    if items:
        header = _items_to_header(items)
        if header:
            return header
    return _read_raw_cookie()


def _merge_set_cookie(items: list[dict], set_cookie_headers: list[str]) -> int:
    """把 Set-Cookie 头合并回 cookie 列表，返回更新/新增条数。"""
    by_name = {c["name"]: c for c in items if isinstance(c, dict) and "name" in c}
    now = time.time()
    changed = 0
    for raw in set_cookie_headers:
        head, *attrs = raw.split(";")
        if "=" not in head:
            continue
        name, value = head.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        max_age = None
        for a in attrs:
            k, _, v = a.strip().partition("=")
            if k.lower() == "max-age":
                try:
                    max_age = int(v)
                except ValueError:
                    max_age = None
        entry = by_name.get(name)
        if entry is None:
            entry = {"domain": ".doubao.com", "hostOnly": False, "httpOnly": True,
                     "name": name, "path": "/", "sameSite": "no_restriction",
                     "secure": True, "session": False, "storeId": None}
            items.append(entry)
            by_name[name] = entry
        entry["value"] = value
        if max_age is not None:
            entry["expirationDate"] = now + max_age
        changed += 1
    return changed


def _beat(cookie_header: str, timeout: float) -> tuple[bool, str, list[str]]:
    """调用心跳端点。返回 (登录态是否有效, 说明, Set-Cookie 头列表)。"""
    conn = http.client.HTTPSConnection(RENEW_HOST, timeout=timeout)
    try:
        conn.request("GET", RENEW_PATH, headers={
            "Cookie": cookie_header,
            "Origin": "https://www.doubao.com",
            "Referer": "https://www.doubao.com/chat/",
            "User-Agent": _UA,
        })
        resp = conn.getresponse()
        body = resp.read()
        set_cookies = [v for k, v in resp.getheaders() if k.lower() == "set-cookie"]
        status = resp.status
    except (OSError, http.client.HTTPException) as e:
        return False, f"请求失败: {type(e).__name__}: {e}", []
    finally:
        conn.close()

    if status != 200:
        return False, f"HTTP {status}", []
    try:
        msg = json.loads(body).get("message", "")
    except (json.JSONDecodeError, AttributeError):
        msg = body[:80].decode("utf-8", "replace")
    if msg != "success":
        return False, f"登录态无效: {msg}", []
    return True, "success", set_cookies


def sync_cookie_files(timeout: float = 30.0) -> tuple[bool, str]:
    """启动时对齐两份 cookie 文件。

    不猜哪个更新，直接让服务端判定：
      1. 先用 .cookie 的内容去续期，成功就以它为准对齐两个文件
      2. 失败则改用 .doubao_cookie，成功同样对齐
      3. 都失败则报错，提示用户重新导出

    返回 (是否可用, 说明文字)。
    """
    raw = _read_raw_cookie()
    items = _read_json_items()
    json_header = _items_to_header(items) if items else ""

    if not raw and not json_header:
        return False, (f"未找到有效 cookie。请把浏览器里的 Cookie 头保存到 "
                       f"{RAW_COOKIE_FILE.name}，或把扩展导出的 JSON 保存到 "
                       f"{COOKIE_FILE.name}")

    # 两边一致时也要验证有效性——两份可能同时失效（如账号在别处登出）
    aligned = bool(raw) and bool(json_header) and raw == json_header
    if aligned:
        ok, msg, set_cookies = _beat(raw, timeout)
        if not ok:
            return False, (f"两份 cookie 内容一致但已失效，请重新从浏览器获取最新 cookie。\n"
                           f"   {msg}")
        if set_cookies:
            merged = list(items or _header_to_items(raw))
            n = _merge_set_cookie(merged, set_cookies)
            if n:
                try:
                    _persist_both(merged)
                except OSError as e:
                    return False, f"写入失败: {type(e).__name__}: {e}"
        left = cookie_expiry_days()
        tail = f"，剩余 {left:.1f} 天" if left is not None else ""
        return True, f"两份 cookie 文件已一致且有效{tail}"

    # 按优先级依次验证：.cookie 优先（用户手动更新的入口）
    attempts = [(RAW_COOKIE_FILE.name, raw), (COOKIE_FILE.name, json_header)]
    errors = []
    for label, header in attempts:
        if not header:
            continue
        ok, msg, set_cookies = _beat(header, timeout)
        if not ok:
            errors.append(f"{label}: {msg}")
            continue
        # 这份有效：以它的值为准，并沿用 JSON 里已有的 expirationDate 等元数据
        merged = _header_to_items(header, items)
        n = _merge_set_cookie(merged, set_cookies)
        try:
            _persist_both(merged)
        except OSError as e:
            return False, f"对齐写入失败: {type(e).__name__}: {e}"
        left = cookie_expiry_days(merged)
        tail = f"，剩余 {left:.1f} 天" if left is not None else ""
        extra = f"，续期 {n} 项" if n else ""
        return True, f"已用 {label} 对齐两份文件{extra}{tail}"

    return False, ("两份 cookie 都无法通过验证，请重新从浏览器获取最新 cookie。\n"
                   "   " + "\n   ".join(errors))


def renew_cookie(timeout: float = 30.0) -> tuple[bool, str]:
    """调用心跳端点续期登录态，并把新 cookie 写回两个文件。不抛异常。"""
    items = _read_json_items()
    header = _items_to_header(items) if items else _read_raw_cookie()
    if not header:
        return False, "未找到 cookie 文件"

    ok, msg, set_cookies = _beat(header, timeout)
    if not ok:
        if "登录态无效" in msg:
            return False, f"登录已失效，需重新导出 cookie（{msg}）"
        return False, msg

    got = {s.split("=", 1)[0].strip() for s in set_cookies}
    if not got & set(CORE_COOKIES):
        # 端点有节流：刚续过或还很新时不重发核心 cookie。这不是失败，只是无需续期。
        left = cookie_expiry_days(items)
        tail = f"，剩余 {left:.1f} 天" if left is not None else ""
        return True, f"登录态有效，服务端本次未下发新 cookie（无需续期）{tail}"

    merged = items if items else _header_to_items(header)
    n = _merge_set_cookie(merged, set_cookies)
    try:
        _persist_both(merged)
    except OSError as e:
        return False, f"写回失败: {type(e).__name__}: {e}"

    left = cookie_expiry_days(merged)
    tail = f"，核心登录态剩余 {left:.1f} 天" if left is not None else ""
    return True, f"已续期 {n} 个 cookie{tail}（两份文件已同步）"


def save_cookie_to_file(cookie: str):
    """保存用户提供的原始 Cookie 头，并同步生成 JSON 版本。"""
    header = cookie.strip()
    items = _header_to_items(header, _read_json_items())
    try:
        _persist_both(items)
    except OSError as e:
        print(f"❌ 保存失败: {type(e).__name__}: {e}")
        return
    print(f"✅ Cookie 已保存到 {RAW_COOKIE_FILE.name} 和 {COOKIE_FILE.name}")
    print("   提示：纯文本没有过期时间，建议再跑一次 "
          "python renew_cookie.py 以获取准确的过期信息")


async def main():
    parser = argparse.ArgumentParser(description="豆包 TTS 文本转语音工具")
    parser.add_argument("text", nargs="?", default="", help="要转换的文本")
    parser.add_argument("-o", "--output", default="output.mp3", help="输出文件路径")
    parser.add_argument("-s", "--speaker", default="taozi",
                        help=f"语音角色（简称/speaker_id），简称: {', '.join(SPEAKERS)}")
    parser.add_argument("--speed", type=float, default=1.0, help="语速倍率 (0.5 ~ 2.0, 1.0 为正常)")
    parser.add_argument("--pitch", type=float, default=0, help="音调 (-12 ~ 12 半音, 0 为正常)")
    parser.add_argument("--format", default="mp3",
                        choices=["mp3", "ogg_opus", "wav", "pcm"], help="音频格式")
    parser.add_argument("--list-speakers", action="store_true", help="列出可用音色")
    parser.add_argument("--search", metavar="关键词",
                        help="按名称/标签/ID 模糊查找音色")
    parser.add_argument("--cookie", help="豆包网站 Cookie (首次使用需要)")
    parser.add_argument("--save-cookie", action="store_true", help="保存 Cookie 到配置文件")

    args = parser.parse_args()

    if args.list_speakers or args.search:
        catalog = voice_catalog()
        kw = (args.search or "").strip().lower()
        if kw:
            catalog = [v for v in catalog
                       if kw in v.get("speaker_id", "").lower()
                       or kw in (v.get("name") or "").lower()
                       or any(kw in t.lower() for t in v.get("tags") or [])]
            print(f"\n匹配 “{args.search}” 的音色（{len(catalog)} 个）:")
        else:
            print(f"\n可用音色（共 {len(catalog)} 个）:")
        print("-" * 88)
        alias_of = {sid: a for a, sid in SPEAKERS.items()}
        for v in catalog:
            sid = v.get("speaker_id", "")
            alias = alias_of.get(sid, "")
            tags = ",".join((v.get("tags") or [])[:3])
            print(f"  {(v.get('name') or '')[:12]:<14} {sid:<46} "
                  f"{alias:<11} {tags}")
        print("-" * 88)
        if not VOICES_FILE.exists():
            print("⚠️  未找到 voices.json，仅显示内置简称。"
                  "运行 python fetch_voices.py 拉取完整列表。")
        return

    # 处理 cookie
    cookie = args.cookie or load_cookie_from_file()

    if args.save_cookie and args.cookie:
        save_cookie_to_file(args.cookie)

    if not cookie:
        print("⚠️  需要提供 Cookie 才能使用豆包 TTS")
        print("\n获取方法:")
        print("  1. 打开浏览器访问 https://www.doubao.com 并登录")
        print("  2. 按 F12 打开开发者工具")
        print("  3. 切换到 Network 标签页")
        print("  4. 刷新页面，点击任意请求")
        print("  5. 在 Headers 中找到 Cookie 并复制")
        print("\n使用方法:")
        print('  python doubao_tts.py "文本" --cookie "你的cookie" --save-cookie')
        return

    if not args.text:
        parser.print_help()
        return

    # 服务端对未知音色会静默回退默认值，这里先警告避免“成功但音色不对”
    if resolve_speaker(args.speaker) is None:
        print(f"⚠️  音色 {args.speaker!r} 不在音色表中，服务端可能静默回退默认音色。\n"
              f"   用 --list-speakers 或 --search 关键词 查看可用音色。")
    config = TTSConfig(format=args.format, cookie=cookie)
    tts = DoubaoTTS(config)
    tts.set_speaker(args.speaker)
    tts.set_speed(args.speed)
    tts.set_pitch(args.pitch)

    print("\n🎤 豆包 TTS")
    print(f"   文本: {args.text[:50]}{'...' if len(args.text) > 50 else ''}")
    print(f"   语音: {args.speaker}")
    print(f"   输出: {args.output}\n")

    # 合成语音
    result = await tts.synthesize(args.text)

    if result.success:
        # 保存文件
        output_path = Path(args.output)
        output_path.write_bytes(result.audio_data)
        print(f"\n✅ 已保存到: {output_path.absolute()}")
        print(f"   文件大小: {len(result.audio_data):,} bytes")
    else:
        print(f"\n❌ 合成失败: {result.error}")


def _cli():
    """同步入口，供 console_scripts / uv run 使用（main 是 async）。"""
    asyncio.run(main())


if __name__ == "__main__":
    _cli()
