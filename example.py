#!/usr/bin/env python3
"""
豆包 TTS 使用示例
"""

import asyncio
from pathlib import Path

from doubao_tts import DoubaoTTS


async def example_basic():
    """基础示例"""
    print("\n=== 基础示例 ===")

    tts = DoubaoTTS()
    result = await tts.synthesize("你好，欢迎使用豆包文本转语音服务。")

    if result.success:
        Path("example_basic.mp3").write_bytes(result.audio_data)
        print(f"✅ 保存成功: example_basic.mp3 ({len(result.audio_data):,} bytes)")
    else:
        print(f"❌ 失败: {result.error}")


async def example_different_speakers():
    """不同语音角色示例"""
    print("\n=== 不同语音角色示例 ===")

    text = "大家好，我是豆包智能助手。"

    for name in ["taozi", "yangguang", "rap"]:
        tts = DoubaoTTS()
        tts.set_speaker(name)

        result = await tts.synthesize(text)
        if result.success:
            filename = f"example_{name}.mp3"
            Path(filename).write_bytes(result.audio_data)
            print(f"✅ {name}: {filename}")


async def example_speed_pitch():
    """语速和音调示例"""
    print("\n=== 语速和音调示例 ===")

    text = "调整语速和音调可以让声音更有特色。"

    # 新协议: speed 是倍率(1.0 正常), pitch 是半音(-12~12)
    configs = [
        ("normal", 1.0, 0),
        ("fast", 1.5, 0),
        ("slow", 0.7, 0),
        ("high_pitch", 1.0, 3),
        ("low_pitch", 1.0, -3),
    ]

    for name, speed, pitch in configs:
        tts = DoubaoTTS()
        tts.set_speed(speed).set_pitch(pitch)

        result = await tts.synthesize(text)
        if result.success:
            filename = f"example_{name}.mp3"
            Path(filename).write_bytes(result.audio_data)
            print(f"✅ {name} (speed={speed}, pitch={pitch}): {filename}")


async def example_streaming():
    """流式处理示例"""
    print("\n=== 流式处理示例 ===")

    tts = DoubaoTTS()

    chunk_count = 0
    total_bytes = 0

    def on_chunk(data: bytes):
        nonlocal chunk_count, total_bytes
        chunk_count += 1
        total_bytes += len(data)

    def on_sentence(text: str):
        print(f"  📢 朗读: {text[:30]}...")

    long_text = """
    人工智能正在改变我们的生活方式。
    从智能助手到自动驾驶，从医疗诊断到金融分析。
    未来将会有更多令人兴奋的应用场景出现。
    """

    result = await tts.synthesize(
        long_text,
        on_audio_chunk=on_chunk,
        on_sentence_start=on_sentence,
    )

    if result.success:
        print("✅ 流式处理完成:")
        print(f"   - 音频块数: {chunk_count}")
        print(f"   - 总字节数: {total_bytes:,}")
        print(f"   - 句子数: {len(result.sentences)}")

        Path("example_streaming.mp3").write_bytes(result.audio_data)


async def example_english():
    """英文语音示例"""
    print("\n=== 英文语音示例 ===")

    tts = DoubaoTTS()
    tts.set_speaker("en_female")

    result = await tts.synthesize(
        "Hello! Welcome to Doubao Text-to-Speech service. "
        "This is an example of English voice synthesis."
    )

    if result.success:
        Path("example_english.mp3").write_bytes(result.audio_data)
        print("✅ 保存成功: example_english.mp3")


async def main():
    """运行所有示例"""
    print("🎤 豆包 TTS 示例程序")
    print("=" * 50)

    await example_basic()
    await example_different_speakers()
    await example_speed_pitch()
    await example_streaming()
    await example_english()

    print("\n" + "=" * 50)
    print("✨ 所有示例完成!")
    print("   生成的音频文件可以用任意播放器打开")


if __name__ == "__main__":
    asyncio.run(main())
