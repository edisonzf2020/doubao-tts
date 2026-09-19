#!/usr/bin/env python3
"""
豆包 cookie 保温 / 对齐工具

两份文件始终保持同步:
    .cookie         原始 Cookie 头字符串（从浏览器 DevTools 复制的格式）
    .doubao_cookie  JSON 数组（带过期时间，用于判断剩余天数）

核心登录态有效期约 30 天，但字节的心跳端点 /passport/token/beat/v2/
会把它滚动续期到 ~29.85 天。只要定期调用，登录态就不会过期。

用法:
    python renew_cookie.py                      # 对齐 + 按需续期
    python renew_cookie.py --status             # 只看状态
    python renew_cookie.py --sync               # 只对齐两份文件
    python renew_cookie.py --force              # 强制调一次心跳
    python renew_cookie.py --set-cookie "k=v; ..."   # 保存新 cookie（失效后用）

首次配置或 cookie 失效后:
    1. 浏览器登录豆包 -> F12 -> Network -> 任意请求 -> 复制 Cookie 请求头
    2. python renew_cookie.py --set-cookie "粘贴的内容"
       （或直接把内容写进 .cookie 文件）

配合 cron 每天跑一次即可永不过期:
    0 4 * * * cd /path/to/doubao-tts && .venv/bin/python renew_cookie.py --quiet >> renew.log 2>&1
"""

import argparse
import sys

from doubao_tts import (
    COOKIE_FILE,
    RAW_COOKIE_FILE,
    cookie_expiry_days,
    load_cookie_from_file,
    renew_cookie,
    save_cookie_to_file,
    sync_cookie_files,
)

# 默认阈值：剩余少于 25 天就续期。
# 心跳端点有节流（同一 session 刚续过不会重发），所以每天跑也不会有副作用。
DEFAULT_THRESHOLD = 25.0

EPILOG = """首次配置或 cookie 失效后:
  1. 浏览器登录豆包 -> F12 -> Network -> 任意请求 -> 复制 Cookie 请求头
  2. python renew_cookie.py --set-cookie "粘贴的内容"
     （或直接把内容写进 .cookie 文件）

cron 每天跑一次即可永不过期:
  0 4 * * * cd /path/to/doubao-tts && .venv/bin/python renew_cookie.py --quiet
"""


def show_status() -> int:
    raw_exists = RAW_COOKIE_FILE.exists() and RAW_COOKIE_FILE.read_text().strip()
    json_exists = COOKIE_FILE.exists()
    print(f"{RAW_COOKIE_FILE.name:16} {'有数据' if raw_exists else '空/缺失'}")
    print(f"{COOKIE_FILE.name:16} {'有数据' if json_exists else '空/缺失'}")

    if not load_cookie_from_file():
        print("\n❌ 没有可用 cookie，请先配置（见 --help）")
        return 1

    days = cookie_expiry_days()
    if days is None:
        print("\n剩余天数: 未知（纯文本没有过期信息，跑一次续期即可补齐）")
    elif days < 0:
        print(f"\n🔴 核心登录态已过期 {-days:.1f} 天，需重新获取 cookie")
        return 1
    else:
        print(f"\n核心登录态剩余 {days:.2f} 天")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="豆包 cookie 保温 / 对齐",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG)
    ap.add_argument("--status", action="store_true", help="只显示状态，不改动")
    ap.add_argument("--sync", action="store_true", help="只对齐两份文件，不看阈值")
    ap.add_argument("--force", action="store_true", help="忽略阈值，强制调用心跳")
    ap.add_argument("--set-cookie", metavar="COOKIE",
                    help="保存新的原始 Cookie 头（同时写入两份文件）")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"剩余天数低于此值才续期（默认 {DEFAULT_THRESHOLD}）")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="无需续期时不输出（适合 cron）")
    args = ap.parse_args()

    # 保存新 cookie：失效后用户重新获取的入口
    if args.set_cookie:
        save_cookie_to_file(args.set_cookie)
        ok, msg = renew_cookie()
        print(f"{'✅' if ok else '⚠️ '} {msg}")
        return 0 if ok else 1

    if args.status:
        return show_status()

    if not load_cookie_from_file():
        print(f"❌ 未找到可用 cookie。请把浏览器的 Cookie 头写入 "
              f"{RAW_COOKIE_FILE.name}，或用 --set-cookie 保存",
              file=sys.stderr)
        return 1

    # 对齐两份文件（内容不一致时由服务端判定哪份有效）
    ok, msg = sync_cookie_files()
    if not ok:
        print(f"❌ {msg}", file=sys.stderr)
        return 1
    if args.sync:
        print(f"✅ {msg}")
        return 0
    if not args.quiet and "已一致" not in msg:
        print(f"✓ {msg}")

    days = cookie_expiry_days()
    if days is not None and days < 0:
        print(f"❌ 核心登录态已过期 {-days:.1f} 天，需重新获取 cookie",
              file=sys.stderr)
        return 1

    # 天数未知（纯文本）时也续一次，好把过期信息补齐
    need = args.force or days is None or days < args.threshold
    if not need:
        if not args.quiet:
            print(f"剩余 {days:.2f} 天 ≥ 阈值 {args.threshold:.0f} 天，无需续期")
        return 0

    ok, msg = renew_cookie()
    if ok:
        print(f"✅ {msg}")
        return 0
    print(f"❌ 续期失败: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
