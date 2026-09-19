"""离线冒烟测试：不需要 cookie，不触网。

覆盖纯逻辑单元，克隆后立刻可跑，是防接口/协议回归的第一道防线：
  - protobuf 编解码往返
  - 格式映射与 media type
  - 参数 clamp（speed / pitch）
  - 音色解析白名单
  - 模块可导入

用法:
    python test_smoke.py        # 或 pytest test_smoke.py
"""
import sys


def _fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def test_imports() -> None:
    """所有模块可导入（防 ImportError 回归）。"""
    import doubao_tts  # noqa: F401
    import fetch_voices  # noqa: F401
    import openai_server  # noqa: F401
    import renew_cookie  # noqa: F401
    print("  [imports] 4 个模块导入成功")


def test_protobuf_roundtrip() -> None:
    """手写 protobuf 编解码往返一致。"""
    from doubao_tts import _decode_response, _encode_request

    # 编码请求，再按响应 schema 解不同——所以单独构造响应字节验证解码。
    # 这里验证 varint/字段编号逻辑：编码后能被自己的 wire 解析器读回。
    raw = _encode_request(appkey="AK", namespace="VoiceGenie",
                          event="StartTask", task_id="t-123")
    # _encode_request 用 request schema（appkey=2 namespace=3 event=5 task_id=8）,
    # _decode_response 用 response schema（namespace=3 event=4 ...）——字段号不同,
    # 所以只验证：编码非空、能被 wire 解析器无异常读完。
    if not raw or not isinstance(raw, bytes):
        _fail("encode_request 返回空或非 bytes")
    decoded = _decode_response(raw)  # 不校验语义，只要求不抛异常
    if not isinstance(decoded, dict):
        _fail("decode_response 未返回 dict")

    # 用响应 schema 的字段号构造一帧，验证解码语义正确
    # event=4(tag=0x22) 长度前缀字符串 "TaskStarted"
    def _sfield(num, val):
        b = val.encode()
        # tag = num<<3 | 2(LEN)
        return bytes([num << 3 | 2, len(b)]) + b

    frame = _sfield(4, "TaskStarted") + _sfield(1, "task-xyz")
    d = _decode_response(frame)
    if d.get("event") != "TaskStarted":
        _fail(f"decode event 错误: {d.get('event')}")
    if d.get("task_id") != "task-xyz":
        _fail(f"decode task_id 错误: {d.get('task_id')}")
    print("  [protobuf] 编码非空 + 响应帧解码语义正确")


def test_format_map() -> None:
    """OpenAI 格式 -> 豆包格式映射，及不支持格式的处理。"""
    import openai_server as s

    assert s.FORMAT_MAP["mp3"] == "mp3"
    assert s.FORMAT_MAP["opus"] == "ogg_opus"
    assert s.FORMAT_MAP["wav"] == "wav"
    assert s.FORMAT_MAP["pcm"] == "pcm"
    assert "aac" in s.UNSUPPORTED_FORMATS
    assert "flac" in s.UNSUPPORTED_FORMATS
    # 每个支持格式都要有 media type
    for doubao_fmt in s.FORMAT_MAP.values():
        if doubao_fmt not in s.MEDIA_TYPES:
            _fail(f"{doubao_fmt} 缺少 media type")
    print("  [format] 映射与 media type 完整")


def test_param_clamp() -> None:
    """speed / pitch 的范围截断。"""
    from doubao_tts import DoubaoTTS, TTSConfig

    tts = DoubaoTTS(TTSConfig())
    # speed: 0.5 ~ 2.0
    assert tts.set_speed(3.0).config.speech_rate == 2.0
    assert tts.set_speed(0.1).config.speech_rate == 0.5
    assert tts.set_speed(1.3).config.speech_rate == 1.3
    # 非法值回退 1.0
    assert tts.set_speed("bad").config.speech_rate == 1.0  # type: ignore[arg-type]
    # pitch: -12 ~ 12 且取整
    assert tts.set_pitch(99).config.pitch == 12
    assert tts.set_pitch(-99).config.pitch == -12
    assert tts.set_pitch(3.0).config.pitch == 3
    assert tts.set_pitch("bad").config.pitch == 0  # type: ignore[arg-type]
    print("  [clamp] speed/pitch 截断与回退正确")


def test_voice_resolution() -> None:
    """音色解析白名单（离线，只依赖 voices.json / 内置简称）。"""
    from doubao_tts import SPEAKERS, resolve_speaker, voice_catalog

    # 内置简称必然可解析
    for alias in SPEAKERS:
        if resolve_speaker(alias) is None:
            _fail(f"内置简称 {alias} 解析失败")
    # 简称指向的 speaker_id 本身也要可解析
    assert resolve_speaker("zh_female_vv_uranus_bigtts") == "zh_female_vv_uranus_bigtts"
    # 无效值 / OpenAI 预设名 / 空值 返回 None
    for bad in ("不存在的音色xyz", "alloy", "", "zh_typo_bigtts"):
        if resolve_speaker(bad) is not None:
            _fail(f"无效音色 {bad!r} 不应解析成功")
    # 目录至少包含内置简称数量
    if len(voice_catalog()) < len(SPEAKERS):
        _fail("voice_catalog 少于内置简称数")
    print(f"  [voice] 白名单正确，目录 {len(voice_catalog())} 个音色")


def main() -> None:
    print("=== 离线冒烟测试 ===")
    for fn in (test_imports, test_protobuf_roundtrip, test_format_map,
               test_param_clamp, test_voice_resolution):
        fn()
    print("\n全部通过（无需 cookie / 网络）")


if __name__ == "__main__":
    main()
