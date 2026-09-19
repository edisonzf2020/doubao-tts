#!/usr/bin/env python3
"""
从豆包拉取完整音色列表，生成 voices.json

端点: POST https://www.doubao.com/alice/user_voice/recommend
需要 .cookie 里的登录态。

用法:
    python fetch_voices.py            # 拉取并写入 voices.json
    python fetch_voices.py --dry-run  # 只打印统计，不写文件
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from doubao_tts import load_cookie_from_file

URL = "https://www.doubao.com/alice/user_voice/recommend"
OUT = Path(__file__).parent / "voices.json"

# 前端 s2-voice-selector 的分页配置：
#   recommend_type=1  -> 推荐 tab（无 tab_key）
#   recommend_type=10 -> 分类 tab（female / male / characters / accent）
TABS = [
    ("recommend", 1, None),
    ("female", 10, "female"),
    ("male", 10, "male"),
    ("characters", 10, "characters"),
    ("accent", 10, "accent"),
]
PAGE_SIZE = 30
MAX_PAGES = 60  # 安全上限，防止服务端 has_more 一直为 true 导致死循环


def _post(cookie: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://www.doubao.com",
            "Referer": "https://www.doubao.com/chat/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/153.0.0.0 Safari/537.36",
            "Cookie": cookie,
        },
    )
    # 绕过系统代理，和 doubao_tts 的 WebSocket 保持一致
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"非 JSON 响应（登录可能失效）: {raw[:120]!r}") from None


def fetch_tab(cookie: str, label: str, rtype: int, tab_key: str | None) -> list[dict]:
    """拉取单个 tab 的全部分页。"""
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        payload: dict = {
            "page_index": page,
            "page_size": PAGE_SIZE,
            "recommend_type": rtype,
        }
        if tab_key:
            payload["tab_key"] = tab_key
        try:
            data = _post(cookie, payload)
        except urllib.error.HTTPError as e:
            print(f"  [{label}] page {page} HTTP {e.code}", file=sys.stderr)
            break
        except Exception as e:  # noqa: BLE001 - 网络层异常都只中止该 tab
            print(f"  [{label}] page {page} {type(e).__name__}: {e}", file=sys.stderr)
            break

        if data.get("code") not in (0, None):
            print(f"  [{label}] page {page} code={data.get('code')} "
                  f"msg={data.get('msg') or data.get('message')}", file=sys.stderr)
            break

        d = data.get("data") or {}
        items = d.get("ugc_voice_list") or []
        out.extend(items)
        print(f"  [{label}] page {page}: +{len(items)} (累计 {len(out)})")
        if not d.get("has_more") or not items:
            break
    return out


def normalize(raw: list[dict], tab: str) -> list[dict]:
    """抽取需要的字段。style_id 才是 TTS 用的 speaker ID。"""
    out = []
    for v in raw:
        style_id = (v.get("style_id") or "").strip()
        if not style_id:
            continue
        # 前端会过滤掉 BV 开头的小模型音色
        if style_id.startswith("BV"):
            continue
        out.append({
            "speaker_id": style_id,
            "name": (v.get("name") or "").strip(),
            "voice_id": v.get("id") or "",
            "tags": [t.get("tag_value", "") for t in (v.get("tag_list") or [])
                     if t.get("tag_value")],
            "tab": tab,
            "language": v.get("language_code") or "",
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="拉取豆包完整音色列表")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args()

    cookie = load_cookie_from_file()
    if not cookie:
        print("❌ 未找到 cookie（.cookie / .doubao_cookie）", file=sys.stderr)
        raise SystemExit(1)

    print(f"拉取音色列表 -> {URL}")
    by_id: dict[str, dict] = {}
    for label, rtype, tab_key in TABS:
        raw = fetch_tab(cookie, label, rtype, tab_key)
        for v in normalize(raw, label):
            sid = v["speaker_id"]
            if sid in by_id:
                # 同一音色可能出现在多个 tab，合并 tab 标记
                prev = by_id[sid]
                tabs = set(prev["tab"].split(",")) | {v["tab"]}
                prev["tab"] = ",".join(sorted(tabs))
            else:
                by_id[sid] = v

    voices = sorted(by_id.values(), key=lambda x: (x["tab"], x["speaker_id"]))
    print(f"\n去重后共 {len(voices)} 个音色")

    if not voices:
        print("❌ 未获取到任何音色，检查 cookie 是否有效", file=sys.stderr)
        raise SystemExit(1)

    if args.dry_run:
        for v in voices[:20]:
            print(f"  {v['speaker_id']:52} {v['name']}")
        if len(voices) > 20:
            print(f"  … 还有 {len(voices) - 20} 个")
        return

    OUT.write_text(
        json.dumps({"voices": voices}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"✅ 已写入 {OUT} ({len(voices)} 个音色)")


if __name__ == "__main__":
    main()
