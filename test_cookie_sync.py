"""校验两份 cookie 文件的对齐逻辑（会触网调用心跳端点）。

覆盖场景:
  1. 只有 .doubao_cookie（JSON）  -> 自动补出 .cookie
  2. 只有 .cookie（纯文本）       -> 自动补出 .doubao_cookie，并能算出剩余天数
  3. 两份内容不一致              -> 用能通过服务端验证的那份对齐
  4. 两份都无效                  -> 明确报错提示重新导出
  5. 两份都不存在                -> 明确报错

测试会临时改写 cookie 文件，结束时无条件恢复原始内容。
需要 .doubao_cookie 里有活跃 cookie 才能跑（前 3 个场景要真实续期）。

用法:
    python test_cookie_sync.py
"""
import json
import shutil
import sys
from pathlib import Path
from typing import NoReturn

from doubao_tts import (
    COOKIE_FILE,
    RAW_COOKIE_FILE,
    cookie_expiry_days,
    load_cookie_from_file,
    sync_cookie_files,
)

HERE = Path(__file__).parent
BAK_JSON = HERE / ".doubao_cookie.synctest"
BAK_RAW = HERE / ".cookie.synctest"


def fail(msg: str) -> NoReturn:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def read_raw() -> str:
    return (RAW_COOKIE_FILE.read_text(encoding="utf-8").strip()
            if RAW_COOKIE_FILE.exists() else "")


def read_items() -> list:
    if not COOKIE_FILE.exists():
        return []
    try:
        d = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return d if isinstance(d, list) else []


def header_from_items(items: list) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in items
                     if isinstance(c, dict) and "name" in c and "value" in c)


def assert_aligned(label: str) -> None:
    raw = read_raw()
    built = header_from_items(read_items())
    if not raw or not built:
        fail(f"{label}: 有一份为空 (raw={len(raw)}, json={len(built)})")
    if raw != built:
        fail(f"{label}: 两份内容不一致\n  raw ={raw[:90]}\n  json={built[:90]}")
    print(f"  ✓ {label}: 两份已对齐（{len(raw)} 字符）")


def main() -> None:
    if not COOKIE_FILE.exists():
        fail(f"缺少 {COOKIE_FILE.name}，无法测试")

    print("=== 场景 1: 只有 JSON，.cookie 缺失 ===")
    RAW_COOKIE_FILE.unlink(missing_ok=True)
    ok, msg = sync_cookie_files()
    print(f"  {msg}")
    if not ok:
        fail(f"场景1 应成功: {msg}")
    assert_aligned("场景1")

    print("\n=== 场景 2: 只有纯文本 .cookie，JSON 缺失 ===")
    COOKIE_FILE.unlink(missing_ok=True)
    ok, msg = sync_cookie_files()
    print(f"  {msg}")
    if not ok:
        fail(f"场景2 应成功: {msg}")
    assert_aligned("场景2")
    days = cookie_expiry_days()
    if days is None:
        fail("场景2: 纯文本转 JSON 后应能算出剩余天数（靠 sid_guard 推算）")
    print(f"  ✓ 场景2: 剩余天数可算出 = {days:.2f} 天")

    print("\n=== 场景 3: 两份不一致（.cookie 被改坏）===")
    RAW_COOKIE_FILE.write_text("sessionid_ss=bogus; uid_tt_ss=bogus\n",
                               encoding="utf-8")
    ok, msg = sync_cookie_files()
    print(f"  {msg}")
    if not ok:
        fail(f"场景3 应回退到有效的那份: {msg}")
    assert_aligned("场景3")
    if "bogus" in read_raw():
        fail("场景3: 无效内容不应被写回")
    print("  ✓ 场景3: 已用有效的那份覆盖无效内容")

    print("\n=== 场景 4: 两份都无效 ===")
    RAW_COOKIE_FILE.write_text("sessionid_ss=bad; uid_tt_ss=bad\n", encoding="utf-8")
    COOKIE_FILE.write_text(json.dumps(
        [{"name": "sessionid_ss", "value": "bad"},
         {"name": "uid_tt_ss", "value": "bad"}], indent=4) + "\n", encoding="utf-8")
    ok, msg = sync_cookie_files()
    print(f"  {msg.splitlines()[0]}")
    if ok:
        fail("场景4: 两份都无效时不应报成功")
    if "重新" not in msg:
        fail(f"场景4: 应提示重新获取 cookie，实际: {msg[:80]}")
    print("  ✓ 场景4: 正确报错并提示重新导出")

    print("\n=== 场景 5: 两份都不存在 ===")
    RAW_COOKIE_FILE.unlink(missing_ok=True)
    COOKIE_FILE.unlink(missing_ok=True)
    ok, msg = sync_cookie_files()
    print(f"  {msg.splitlines()[0]}")
    if ok:
        fail("场景5: 无文件时不应报成功")
    if load_cookie_from_file():
        fail("场景5: 无文件时 load_cookie_from_file 应返回空")
    print("  ✓ 场景5: 正确报错")

    print("\n全部场景通过")


if __name__ == "__main__":
    shutil.copy(COOKIE_FILE, BAK_JSON)
    if RAW_COOKIE_FILE.exists():
        shutil.copy(RAW_COOKIE_FILE, BAK_RAW)
    try:
        main()
    finally:
        shutil.copy(BAK_JSON, COOKIE_FILE)
        if BAK_RAW.exists():
            shutil.copy(BAK_RAW, RAW_COOKIE_FILE)
            BAK_RAW.unlink()
        else:
            RAW_COOKIE_FILE.unlink(missing_ok=True)
        BAK_JSON.unlink()
        left = cookie_expiry_days()
        print(f"已恢复原始 cookie: "
              f"{f'{left:.2f} 天' if left is not None else '未知'}")
