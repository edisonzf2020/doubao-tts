#!/usr/bin/env python3
"""
豆包 TTS 的 OpenAI 兼容服务

暴露 POST /v1/audio/speech，协议对齐 OpenAI Audio Speech API，
底层调用 doubao_tts.DoubaoTTS（豆包网页端 VoiceGenie 接口）。

启动:
    export DOUBAO_TTS_API_KEY="sk-your-secret"
    python openai_server.py

调用:
    from openai import OpenAI
    client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-your-secret")
    client.audio.speech.create(
        model="tts-1",
        voice="taozi",
        input="你好，世界",
    ).stream_to_file("out.mp3")
"""

import asyncio
import contextlib
import json
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from doubao_tts import (
    SPEAKERS,
    DoubaoTTS,
    TTSConfig,
    cookie_expiry_days,
    load_cookie_from_file,
    renew_cookie,
    resolve_speaker,
    sync_cookie_files,
    voice_catalog,
)

# ---------------- 配置 ----------------
# 先加载 .env，再读 os.environ。override=False 保证已存在的环境变量优先。
load_dotenv(Path(__file__).parent / ".env", override=False)

API_KEY = os.environ.get("DOUBAO_TTS_API_KEY", "").strip()
# 未设置 API Key 时只绑定本机回环，避免账号被局域网任意设备使用
HOST = os.environ.get("DOUBAO_TTS_HOST", "" if API_KEY else "127.0.0.1") or "127.0.0.1"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读取整型环境变量；非法值回退默认并警告，不让启动崩在栈里。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        print(f"⚠️  {name}={raw!r} 不是整数，使用默认值 {default}", file=sys.stderr)
        return default
    if v < minimum:
        print(f"⚠️  {name}={v} 小于下限 {minimum}，使用 {minimum}", file=sys.stderr)
        return minimum
    return v


PORT = _env_int("DOUBAO_TTS_PORT", 8000)
# 并发上限: 每个请求独占一条 WebSocket，信号量防止打爆豆包配额触发风控
MAX_CONCURRENCY = _env_int("DOUBAO_TTS_CONCURRENCY", 8)
# 单次合成的文本长度上限（信任边界，防止超长输入拖死连接）
MAX_INPUT_CHARS = _env_int("DOUBAO_TTS_MAX_INPUT", 4096)

# ---------------- cookie 保温 ----------------
# 核心登录态约 30 天，心跳端点会滚动续期到 ~29.85 天。
# 默认每 12 小时检查一次，剩余不足 25 天就续。
KEEPALIVE_ENABLED = os.environ.get(
    "DOUBAO_TTS_KEEPALIVE", "1").strip().lower() not in ("0", "false", "no", "off")
KEEPALIVE_INTERVAL_H = _env_int("DOUBAO_TTS_KEEPALIVE_INTERVAL_H", 12)
KEEPALIVE_THRESHOLD_D = _env_int("DOUBAO_TTS_KEEPALIVE_THRESHOLD_D", 25)

# OpenAI response_format -> 豆包 format
# 豆包 VoiceGenie 只支持这四种；OpenAI 的 aac/flac 无对应，明确报错而不是静默降级
FORMAT_MAP = {
    "mp3": "mp3",
    "opus": "ogg_opus",
    "wav": "wav",
    "pcm": "pcm",
}
UNSUPPORTED_FORMATS = {"aac", "flac"}

MEDIA_TYPES = {
    "mp3": "audio/mpeg",
    "ogg_opus": "audio/ogg",
    "wav": "audio/wav",
    "pcm": "audio/L16",
}

# OpenAI 的 6 个标准音色名，豆包没有对应实现，命中时给出可用清单
OPENAI_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer",
                 "ash", "coral", "sage", "ballad", "verse"}

_semaphore: asyncio.Semaphore | None = None
_cookie = ""


# ---------------- 鉴权 ----------------
_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not API_KEY:
        return  # 未配置 key，仅回环可达
    token = creds.credentials if creds else ""
    # 常量时间比较，避免时序侧信道
    if not token or not secrets.compare_digest(token, API_KEY):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Incorrect API key provided.",
                              "type": "invalid_request_error",
                              "code": "invalid_api_key"}},
        )


# ---------------- 请求模型 ----------------
class SpeechRequest(BaseModel):
    # 豆包没有模型概念，接受任意值并忽略，默认 tts-1
    model: str = "tts-1"
    input: str = Field(..., min_length=1)
    voice: str = "taozi"
    response_format: str = "mp3"
    speed: float = 1.0
    # gpt-4o-mini-tts 的参数，豆包 VoiceGenie 无对应能力，接受并忽略
    instructions: str | None = None
    # OpenAI 无此参数，本服务扩展：音调 -12~12 半音
    pitch: int = 0

    @field_validator("input")
    @classmethod
    def _check_input(cls, v: str) -> str:
        if len(v) > MAX_INPUT_CHARS:
            raise ValueError(
                f"input too long: {len(v)} chars (max {MAX_INPUT_CHARS})")
        return v

    @field_validator("response_format")
    @classmethod
    def _check_format(cls, v: str) -> str:
        v = (v or "mp3").lower()
        if v in UNSUPPORTED_FORMATS:
            raise ValueError(
                f"response_format '{v}' is not supported by the Doubao backend. "
                f"Use one of: {', '.join(sorted(FORMAT_MAP))}")
        if v not in FORMAT_MAP:
            raise ValueError(
                f"unknown response_format '{v}'. "
                f"Use one of: {', '.join(sorted(FORMAT_MAP))}")
        return v

    @field_validator("voice")
    @classmethod
    def _check_voice(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("voice is required")
        # 必须命中音色表：服务端对未知 speaker ID 会静默回退默认音色，
        # 不在这里拦住的话用户会拿到“成功但音色不对”的隐形错误。
        if resolve_speaker(v) is not None:
            return v
        if v.lower() in OPENAI_VOICES:
            raise ValueError(
                f"voice '{v}' is an OpenAI preset with no Doubao equivalent. "
                f"Query GET /v1/audio/voices for the {len(voice_catalog())} "
                f"available Doubao voices, or use an alias: "
                f"{', '.join(SPEAKERS)}.")
        raise ValueError(
            f"unknown voice '{v}'. It is not in the Doubao voice catalog "
            f"({len(voice_catalog())} entries). Query GET /v1/audio/voices "
            f"for the full list, or use an alias: {', '.join(SPEAKERS)}.")


# ---------------- 应用 ----------------
async def _cookie_keepalive() -> None:
    """后台保温：定期检查剩余天数，快过期时调心跳续期。

    心跳端点自带节流（同一 session 刚续过不会重发），所以多调无副作用。
    任何异常只警告，不能让服务挂掉。
    """
    global _cookie
    while True:
        try:
            await asyncio.sleep(KEEPALIVE_INTERVAL_H * 3600)
            days = await asyncio.to_thread(cookie_expiry_days)
            if days is None:
                continue
            if days < 0:
                print(f"🔴 cookie 已过期 {-days:.1f} 天，需重新从浏览器导出",
                      file=sys.stderr)
                continue
            if days >= KEEPALIVE_THRESHOLD_D:
                continue
            ok, msg = await asyncio.to_thread(renew_cookie)
            if ok:
                # 续期后 cookie 内容可能已变，重新载入供后续请求使用
                _cookie = await asyncio.to_thread(load_cookie_from_file)
                print(f"✓ cookie 保温: {msg}")
            else:
                print(f"⚠️  cookie 续期失败: {msg}", file=sys.stderr)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 保温失败不能影响服务
            print(f"⚠️  cookie 保温异常: {type(e).__name__}: {e}", file=sys.stderr)


@contextlib.asynccontextmanager
async def lifespan(_app: "FastAPI"):
    global _semaphore, _cookie
    _semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    _cookie = load_cookie_from_file()
    if not _cookie:
        print("⚠️  未找到 cookie（.doubao_cookie / .cookie），请求将全部失败",
              file=sys.stderr)
    if not API_KEY:
        print(f"⚠️  未设置 DOUBAO_TTS_API_KEY，服务不鉴权，仅绑定 {HOST}",
              file=sys.stderr)
    n_voices = len(voice_catalog())
    if n_voices <= len(SPEAKERS):
        print(f"⚠️  只加载到 {n_voices} 个内置音色，未找到 voices.json。\n"
              f"   其余音色会被校验拒结。请先运行: python fetch_voices.py",
              file=sys.stderr)
    else:
        print(f"✓ 已加载 {n_voices} 个音色")

    # 启动时先对齐两份 cookie 文件（由服务端判定哪份有效），再按剩余天数决定是否续期
    sync_ok, sync_msg = sync_cookie_files()
    if sync_ok:
        print(f"✓ cookie: {sync_msg}")
        _cookie = load_cookie_from_file()
    else:
        print(f"🔴 cookie 不可用: {sync_msg}", file=sys.stderr)

    days = cookie_expiry_days()
    if days is None:
        if _cookie:
            print("ℹ 无法判断 cookie 过期时间（纯文本无此信息），"
                  "跑一次续期即可补齐")
    elif days < 0:
        print(f"🔴 cookie 已过期 {-days:.1f} 天，请重新从浏览器导出", file=sys.stderr)
    else:
        print(f"✓ cookie 剩余 {days:.1f} 天")
        if days < KEEPALIVE_THRESHOLD_D:
            ok, msg = renew_cookie()
            if ok:
                _cookie = load_cookie_from_file()
                print(f"✓ cookie 保温: {msg}")
            else:
                print(f"⚠️  cookie 续期失败: {msg}", file=sys.stderr)

    task = None
    if KEEPALIVE_ENABLED and _cookie:
        task = asyncio.create_task(_cookie_keepalive())
        print(f"✓ cookie 保温已开启（每 {KEEPALIVE_INTERVAL_H}h 检查，"
              f"余 {KEEPALIVE_THRESHOLD_D} 天内续期）")
    try:
        yield
    finally:
        # 服务停止时回收后台任务，避免泄漏
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Doubao TTS (OpenAI-compatible)", version="1.0.0",
              lifespan=lifespan)

# demo 试听用的句子（约 20 字）
DEMO_TEXT = "你好呀，这是一段用来试听音色效果的示例语音。"

_UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>豆包 TTS 音色预览</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
         "PingFang SC", "Microsoft YaHei", sans-serif; color: #1a1a1a;
         background: #fff; }
  header { position: sticky; top: 0; background: #fff; z-index: 10;
           border-bottom: 1px solid #eee; padding: 12px 16px;
           display: flex; flex-direction: column; align-items: center; }
  header > * { width: 100%; max-width: 720px; }
           border-bottom: 1px solid #eee; padding: 12px 16px; }
  h1 { font-size: 18px; margin: 0 0 12px; text-align: center; }
  .keybar { display: flex; gap: 8px; align-items: center; }
  .keybar label { flex: none; font-size: 13px; color: #666; white-space: nowrap; }
  .keybar input { flex: 1; padding: 8px 12px; border: 1px solid #ddd;
                  border-radius: 8px; font-size: 14px; }
  .keybar input.ok { border-color: #22c55e; }
  .keybar button { flex: none; padding: 8px 16px; border: none;
                   border-radius: 8px; background: #2563eb; color: #fff;
                   font-size: 14px; cursor: pointer; }
  .keybar button:hover { background: #1d4ed8; }
  .tabs { display: flex; gap: 8px; margin-top: 12px; }
  .tab { flex: 1; padding: 8px; text-align: center; border-radius: 8px;
         background: #f2f2f2; cursor: pointer; font-size: 14px; user-select: none; }
  .tab.active { background: #2563eb; color: #fff; }
  ul { list-style: none; margin: 0 auto; padding: 0; max-width: 720px; }
  li { display: flex; align-items: center; padding: 12px 16px;
       border-bottom: 1px solid #f2f2f2; }
  .info { flex: 1; min-width: 0; }
  .name { font-size: 15px; font-weight: 600; }
  .tags { font-size: 12px; color: #999; margin-top: 3px; }
  .ids { font-size: 11px; color: #bbb; margin-top: 3px; font-family:
         ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         word-break: break-all; }
  .ids code { background: #f5f5f5; padding: 1px 5px; border-radius: 4px;
              margin-right: 6px; color: #666; }
  .play { flex: none; width: 40px; height: 40px; border-radius: 50%;
          border: none; background: #f2f2f2; cursor: pointer; font-size: 18px;
          display: flex; align-items: center; justify-content: center; }
  .play:hover { background: #e5e5e5; }
  .play:disabled { opacity: .4; cursor: not-allowed; }
  .play.loading { animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .hint { padding: 10px 16px; font-size: 13px; color: #b45309;
          background: #fffbeb; max-width: 720px; margin: 0 auto; }
  .empty { padding: 40px; text-align: center; color: #999; }
</style>
</head>
<body>
<header>
  <h1>豆包 TTS 音色预览</h1>
  <div class="keybar">
    <label for="apikey">输入 API KEY</label>
    <input id="apikey" type="password" placeholder="填入 API Key 才能试听"
           autocomplete="off">
    <button id="savekey">确定</button>
  </div>
  <div class="tabs">
    <div class="tab active" data-g="female">女声</div>
    <div class="tab" data-g="male">男声</div>
  </div>
</header>
<div id="hint" class="hint" style="display:none"></div>
<ul id="list"><li class="empty">加载中…</li></ul>
<audio id="player"></audio>
<script>
(function () {
  var data = { female: [], male: [] };
  var cur = "female";
  var playing = null;
  var savedKey = "";
  var listEl = document.getElementById("list");
  var keyEl = document.getElementById("apikey");
  var saveBtn = document.getElementById("savekey");
  var hintEl = document.getElementById("hint");
  var player = document.getElementById("player");

  function saveKey() {
    savedKey = keyEl.value.trim();
    keyEl.classList.toggle("ok", savedKey.length > 0);
    showHint(savedKey ? "API Key 已保存，可以试听了" : "API Key 已清空");
  }
  saveBtn.addEventListener("click", saveKey);
  keyEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter") saveKey();
  });

  function showHint(msg) {
    hintEl.textContent = msg;
    hintEl.style.display = msg ? "block" : "none";
  }

  function render() {
    var arr = data[cur] || [];
    if (!arr.length) { listEl.innerHTML = '<li class="empty">暂无音色</li>'; return; }
    listEl.innerHTML = "";
    arr.forEach(function (v) {
      var li = document.createElement("li");
      var info = document.createElement("div");
      info.className = "info";
      var name = document.createElement("div");
      name.className = "name";
      name.textContent = v.name || v.speaker_id;
      var tags = document.createElement("div");
      tags.className = "tags";
      tags.textContent = (v.tags || []).join(" \u00b7 ");
      var ids = document.createElement("div");
      ids.className = "ids";
      if (v.alias) {
        var a = document.createElement("code");
        a.textContent = "简称：" + v.alias;
        a.title = "简称，点击复制";
        a.onclick = function () { copy(v.alias); };
        ids.appendChild(a);
      }
      var sid = document.createElement("code");
      sid.textContent = "speaker_id：" + v.speaker_id;
      sid.title = "speaker_id，点击复制";
      sid.onclick = function () { copy(v.speaker_id); };
      ids.appendChild(sid);
      info.appendChild(name); info.appendChild(tags); info.appendChild(ids);
      var btn = document.createElement("button");
      btn.className = "play";
      btn.textContent = "\u25b6";
      btn.onclick = function () { demo(v.speaker_id, btn); };
      li.appendChild(info); li.appendChild(btn);
      listEl.appendChild(li);
    });
  }

  function copy(text) {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text);
      showHint("已复制: " + text);
    }
  }

  function demo(speaker, btn) {
    if (!savedKey) {
      showHint("请先在顶部输入 API Key 并点“确定”");
      keyEl.focus();
      return;
    }
    var key = savedKey;
    showHint("");
    if (playing) { playing.classList.remove("loading"); playing.textContent = "\u25b6"; }
    playing = btn;
    btn.classList.add("loading");
    btn.textContent = "\u25cc";
    fetch("/v1/audio/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 "Authorization": "Bearer " + key },
      body: JSON.stringify({ model: "tts-1", voice: speaker,
                            input: DEMO_TEXT, response_format: "mp3" })
    }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          throw new Error(r.status === 401 ? "API Key 错误" : ("合成失败: " + t.slice(0, 120)));
        });
      }
      return r.blob();
    }).then(function (blob) {
      btn.classList.remove("loading"); btn.textContent = "\u25b6";
      var url = URL.createObjectURL(blob);
      player.src = url;
      player.play();
      player.onended = function () { URL.revokeObjectURL(url); };
    }).catch(function (e) {
      btn.classList.remove("loading"); btn.textContent = "\u25b6";
      showHint(e.message);
    });
  }

  var DEMO_TEXT = %DEMO_TEXT%;

  document.querySelectorAll(".tab").forEach(function (t) {
    t.onclick = function () {
      document.querySelectorAll(".tab").forEach(function (x) {
        x.classList.remove("active"); });
      t.classList.add("active");
      cur = t.getAttribute("data-g");
      render();
    };
  });

  fetch("/ui/voices").then(function (r) { return r.json(); })
    .then(function (d) { data = d; render(); })
    .catch(function () {
      listEl.innerHTML = '<li class="empty">音色列表加载失败</li>';
    });
})();
</script>
</body>
</html>
""".replace("%DEMO_TEXT%", json.dumps(DEMO_TEXT, ensure_ascii=False))


@app.get("/ui/voices")
async def ui_voices() -> JSONResponse:
    """UI 专用：按女声/男声分组的音色列表。

    只返回名称/标签等元数据，不触网、不消耗账号资源，故免鉴权；
    真正防滥用的关口是 demo 合成（/v1/audio/speech 必须带 API Key）。
    """
    female, male = [], []
    alias_of = {sid: a for a, sid in SPEAKERS.items()}
    for v in voice_catalog():
        tags = v.get("tags") or []
        if not tags:
            continue  # 内置简称无性别标签，UI 不展示
        entry = {"speaker_id": v["speaker_id"], "name": v.get("name", ""),
                 "tags": tags, "alias": alias_of.get(v["speaker_id"])}
        if tags[0] == "女":
            female.append(entry)
        elif tags[0] == "男":
            male.append(entry)
    return JSONResponse({"female": female, "male": male})


@app.get("/ui", response_class=HTMLResponse)
async def ui_page() -> HTMLResponse:
    """音色预览页面（免鉴权；demo 合成需在页面填 API Key）。"""
    return HTMLResponse(_UI_HTML)




@app.get("/health")
async def health() -> dict:
    days = cookie_expiry_days()
    return {
        "status": "ok",
        "cookie_loaded": bool(_cookie),
        "cookie_expires_in_days": round(days, 2) if days is not None else None,
        "keepalive_enabled": KEEPALIVE_ENABLED and bool(_cookie),
        "auth_required": bool(API_KEY),
        "max_concurrency": MAX_CONCURRENCY,
        "voices_loaded": len(voice_catalog()),
    }


@app.get("/v1/models", dependencies=[Depends(require_auth)])
async def list_models() -> dict:
    """OpenAI 客户端有时会探测模型列表。豆包无模型概念，返回固定别名。"""
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": 0, "owned_by": "doubao"}
                 for m in ("tts-1", "tts-1-hd", "gpt-4o-mini-tts")],
    }


@app.get("/v1/audio/voices", dependencies=[Depends(require_auth)])
async def list_voices(q: str = "", tab: str = "", limit: int = 0) -> dict:
    """本服务扩展：列出可用音色（OpenAI 无此端点）。

    q     按名称/标签/ID 模糊过滤
    tab   按分类过滤（female / male / characters / accent / recommend）
    limit 最多返回条数（0 = 不限）
    """
    items = voice_catalog()
    kw = q.strip().lower()
    if kw:
        items = [v for v in items
                 if kw in v.get("speaker_id", "").lower()
                 or kw in (v.get("name") or "").lower()
                 or any(kw in t.lower() for t in v.get("tags") or [])]
    if tab:
        tk = tab.strip().lower()
        items = [v for v in items if tk in (v.get("tab") or "").lower()]
    total = len(items)
    if limit > 0:
        items = items[:limit]
    alias_of = {sid: a for a, sid in SPEAKERS.items()}
    return {
        "object": "list",
        "total": total,
        "data": [{
            "id": v["speaker_id"],
            "speaker_id": v["speaker_id"],
            "name": v.get("name", ""),
            "alias": alias_of.get(v["speaker_id"]),
            "tags": v.get("tags") or [],
            "tab": v.get("tab", ""),
        } for v in items],
    }


@app.post("/v1/audio/speech", dependencies=[Depends(require_auth)])
async def create_speech(req: SpeechRequest) -> StreamingResponse:
    if not _cookie:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "Doubao cookie not configured on server.",
                              "type": "server_error"}})

    doubao_format = FORMAT_MAP[req.response_format]
    config = TTSConfig(format=doubao_format, cookie=_cookie)
    tts = DoubaoTTS(config)
    tts.set_speaker(req.voice)
    # OpenAI speed 范围 0.25~4.0，豆包只到 0.5~2.0，set_speed 内部会 clamp
    tts.set_speed(req.speed)
    tts.set_pitch(req.pitch)
    # 记录请求参数；speed 同时打印请求值与 clamp 后的实际值
    print(f"[REQ] voice={req.voice} format={req.response_format} "
          f"speed={req.speed}(→{tts.config.speech_rate}) pitch={req.pitch} "
          f"chars={len(req.input)}")

    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    def on_chunk(data: bytes) -> None:
        queue.put_nowait(data)

    async def run() -> None:
        # ponytail: 每请求一条 WebSocket。上限是并发数 == 连接数；
        # 若日后吞吐不够，升级路径是复用连接（一条连接上串行多次 StartSession）。
        assert _semaphore is not None
        async with _semaphore:
            try:
                result = await tts.synthesize(req.input, on_audio_chunk=on_chunk)
                if not result.success:
                    queue.put_nowait(RuntimeError(result.error or "synthesis failed"))
                    return
            except Exception as e:  # noqa: BLE001 - 需转交给流消费端
                queue.put_nowait(e)
                return
        queue.put_nowait(_DONE)

    task = asyncio.create_task(run())

    async def stream():
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    return
                if isinstance(item, BaseException):
                    # 首字节已发出时无法再改状态码，只能中断流
                    raise item
                yield item
        finally:
            # 客户端断连或出错时确保后台任务不泄漏
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    # 先等首个音频块，让鉴权/参数/上游错误能以正确状态码返回
    first = await queue.get()
    if isinstance(first, BaseException):
        if not task.done():
            task.cancel()
        msg = str(first)
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": f"Doubao synthesis failed: {msg}",
                              "type": "upstream_error"}})
    if first is _DONE:
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": "Doubao returned no audio.",
                              "type": "upstream_error"}})

    async def body():
        yield first
        async for chunk in stream():
            yield chunk

    return StreamingResponse(
        body(),
        media_type=MEDIA_TYPES[doubao_format],
        headers={"X-Doubao-Speaker": tts.config.speaker},
    )


def main() -> None:
    import uvicorn

    if not API_KEY and HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"🔴 拒绝启动：未设置 DOUBAO_TTS_API_KEY 却要监听 {HOST}。\n"
              f"   这会让任何能访问该地址的人使用你的豆包账号。\n"
              f"   请设置 DOUBAO_TTS_API_KEY，或改用 HOST=127.0.0.1。",
              file=sys.stderr)
        raise SystemExit(1)

    print("🎤 豆包 TTS OpenAI 兼容服务")
    print(f"   地址: http://{HOST}:{PORT}/v1")
    print(f"   鉴权: {'需要 Bearer API Key' if API_KEY else '关闭（仅回环）'}")
    print(f"   并发: {MAX_CONCURRENCY}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
