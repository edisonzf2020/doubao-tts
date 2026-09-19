"""校验 .env 自动加载与环境变量优先级。

自带临时 .env（跑完恢复原状），可在任何时候直接运行：
    python test_dotenv.py

加载顺序应为: 系统环境变量 > .env > 代码默认值。
"""
import os
import subprocess
from pathlib import Path

PY = ".venv/bin/python"
PROBE = (
    "import openai_server as s;"
    "print(f'{s.API_KEY}|{s.PORT}|{s.MAX_CONCURRENCY}|{s.HOST}')"
)
ENV_FILE = Path(__file__).parent / ".env"
FIXTURE = (
    "DOUBAO_TTS_API_KEY=sk-from-dotenv\n"
    "DOUBAO_TTS_HOST=127.0.0.1\n"
    "DOUBAO_TTS_PORT=8123\n"
    "DOUBAO_TTS_CONCURRENCY=5\n"
    "DOUBAO_TTS_MAX_INPUT=4096\n"
)


def run(extra_env: dict[str, str]) -> tuple[str, ...]:
    env = dict(os.environ)
    # 清掉可能干扰的同名变量，确保只有 .env 和 extra_env 起作用
    for k in list(env):
        if k.startswith("DOUBAO_TTS_"):
            del env[k]
    env.update(extra_env)
    out = subprocess.run([PY, "-c", PROBE], capture_output=True, text=True,
                         env=env, timeout=60, cwd=Path(__file__).parent)
    if out.returncode != 0:
        raise AssertionError(f"probe 失败: {out.stderr[-400:]}")
    return tuple(out.stdout.strip().split("|"))


def main() -> None:
    print("=== 1. 只有 .env ===")
    key, port, conc, host = run({})
    print(f"  API_KEY={key}  PORT={port}  CONCURRENCY={conc}  HOST={host}")
    assert key == "sk-from-dotenv", f".env 未生效: {key}"
    assert port == "8123", f".env PORT 未生效: {port}"
    assert conc == "5", f".env CONCURRENCY 未生效: {conc}"

    print("\n=== 2. 系统环境变量覆盖 .env ===")
    key2, port2, conc2, _ = run({"DOUBAO_TTS_API_KEY": "sk-from-shell",
                                 "DOUBAO_TTS_PORT": "9999"})
    print(f"  API_KEY={key2}  PORT={port2}  CONCURRENCY={conc2}")
    assert key2 == "sk-from-shell", f"环境变量未覆盖 .env: {key2}"
    assert port2 == "9999", f"环境变量未覆盖 PORT: {port2}"
    assert conc2 == "5", f"未被覆盖的项应仍从 .env 读取: {conc2}"

    print("\n=== 3. 非法值回退默认 ===")
    _, _, conc3, _ = run({"DOUBAO_TTS_CONCURRENCY": "abc"})
    print(f"  CONCURRENCY={conc3}")
    assert conc3 == "8", f"非法值应回退默认 8: {conc3}"

    print("\n.env 加载与优先级校验通过")


if __name__ == "__main__":
    had_env = ENV_FILE.exists()
    backup = ENV_FILE.read_text(encoding="utf-8") if had_env else None
    ENV_FILE.write_text(FIXTURE, encoding="utf-8")
    try:
        main()
    finally:
        if backup is not None:
            ENV_FILE.write_text(backup, encoding="utf-8")
        else:
            ENV_FILE.unlink(missing_ok=True)
