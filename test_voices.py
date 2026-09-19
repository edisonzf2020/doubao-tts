"""音色解析与白名单测试（离线，不触网）。

覆盖 resolve_speaker 的各类输入，确保 HTTP 服务的 voice 校验行为可靠：
服务端对未知 speaker ID 会静默回退默认音色，白名单是唯一的拦截点。

用法:
    python test_voices.py
"""
import sys

from doubao_tts import SPEAKERS, resolve_speaker, voice_catalog


def main() -> None:
    catalog = voice_catalog()
    print(f"音色总数: {len(catalog)}")

    cases = [
        # (输入, 是否应解析成功)
        ("taozi", True),                              # 内置简称
        ("zh_female_vv_uranus_bigtts", True),         # 简称指向的 speaker_id
        ("en_male_adam_conversation_bigtts", True),   # 另一个内置 speaker_id
        ("不存在的音色", False),                       # 无效中文
        ("alloy", False),                             # OpenAI 预设名
        ("nova", False),                              # OpenAI 预设名
        ("", False),                                  # 空值
        ("zh_typo_bigtts", False),                    # 拼错的 _bigtts
    ]

    # voices.json 存在时，验证真实音色（中文名 + ICL）也能解析
    if len(catalog) > len(SPEAKERS):
        # 从目录里挑一个带中文名的和一个 ICL_ 的
        named = next((v for v in catalog if v.get("name")
                      and v["speaker_id"] not in SPEAKERS.values()), None)
        icl = next((v for v in catalog
                    if v.get("speaker_id", "").startswith("ICL_")), None)
        if named:
            cases.append((named["name"], True))          # 中文名
            cases.append((named["speaker_id"], True))     # 其 speaker_id
        if icl:
            cases.append((icl["speaker_id"], True))       # ICL 音色

    bad = 0
    for probe, should in cases:
        got = resolve_speaker(probe)
        ok = bool(got) == should
        if not ok:
            bad += 1
        mark = "OK " if ok else "BAD"
        print(f"  [{mark}] {probe!r:38} -> {got}")

    print(f"\n失败 {bad}/{len(cases)}")
    if bad:
        print("resolve_speaker 行为与预期不符", file=sys.stderr)
        raise SystemExit(1)
    print("音色解析校验通过")


if __name__ == "__main__":
    main()
