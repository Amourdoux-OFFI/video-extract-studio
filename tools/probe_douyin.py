# -*- coding: utf-8 -*-
"""抖音解析链路实测脚本。

用法:
    python tools/probe_douyin.py <aweme_id> [--cookie "..."]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def build_cookie() -> str:
    """用 f2 的 TokenManager 现造一份游客 Cookie。"""
    try:
        from f2.apps.douyin.utils import TokenManager, VerifyFpManager

        parts = []
        try:
            parts.append(f"msToken={TokenManager.gen_real_msToken()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] gen_real_msToken 失败: {exc}")
            parts.append(f"msToken={TokenManager.gen_false_msToken()}")
        try:
            parts.append(f"ttwid={TokenManager.gen_ttwid()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] gen_ttwid 失败: {exc}")
        try:
            parts.append(f"webid={TokenManager.gen_webid()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] gen_webid 失败: {exc}")
        try:
            parts.append(f"s_v_web_id={VerifyFpManager.gen_s_v_web_id()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] gen_s_v_web_id 失败: {exc}")
        return "; ".join(parts)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] TokenManager 不可用: {exc}")
        return ""


async def probe(aweme_id: str, cookie: str):
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import PostDetail
    from f2.apps.douyin.utils import ClientConfManager

    print(f"  加密算法 = {ClientConfManager.encryption()}")
    print(f"  cookie 长度 = {len(cookie)}")
    print(f"  cookie 头 = {cookie[:120]}")

    kwargs = {
        "cookie": cookie,
        "headers": {
            "User-Agent": UA,
            "Referer": "https://www.douyin.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        "proxies": {"http://": None, "https://": None},
        "max_tasks": 4,
        "max_connections": 4,
        "max_retries": 2,
        "timeout": 20,
    }

    async with DouyinCrawler(kwargs) as crawler:
        raw = await crawler.fetch_post_detail(PostDetail(aweme_id=aweme_id))
        return raw


def dump(raw) -> None:
    if not raw:
        print("  !! 空响应")
        return
    print(f"  顶层 keys = {list(raw.keys())[:12]}")

    detail = raw.get("aweme_detail") or {}
    if not detail:
        print("  !! 没有 aweme_detail，原始内容前 600 字：")
        print(json.dumps(raw, ensure_ascii=False)[:600])
        return

    print(f"  作品ID   = {detail.get('aweme_id')}")
    print(f"  标题     = {(detail.get('desc') or '')[:60]}")
    print(f"  作者     = {(detail.get('author') or {}).get('nickname')}")
    print(f"  时长(ms) = {detail.get('duration')}")
    print(f"  类型     = {detail.get('aweme_type')}  media_type={detail.get('media_type')}")

    video = detail.get("video") or {}
    print(f"  video keys = {list(video.keys())[:20]}")

    pa = video.get("play_addr") or {}
    urls = pa.get("url_list") or []
    print(f"  play_addr.url_list ({len(urls)} 条):")
    for u in urls[:3]:
        print(f"    {u[:150]}")

    br = video.get("bit_rate") or []
    print(f"  bit_rate 档位 = {len(br)}")
    for i, item in enumerate(br):
        addr = (item.get("play_addr") or {}).get("url_list") or []
        print(
            f"    [{i}] {item.get('gear_name')} "
            f"{item.get('width')}x{item.get('height')} "
            f"fps={item.get('FPS')} bw={item.get('bit_rate')} "
            f"fmt={item.get('format')} codec={item.get('video_codec') if 'video_codec' in item else '-'}"
        )
        for u in addr[:1]:
            print(f"         {u[:150]}")

    images = detail.get("images") or []
    if images:
        print(f"  !! 这是图集，{len(images)} 张图")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("aweme_id")
    ap.add_argument("--cookie", default=None)
    ap.add_argument("--auto-cookie", action="store_true", default=True)
    args = ap.parse_args()

    cookie = args.cookie
    if cookie is None:
        print("[1] 自动生成游客 Cookie ...")
        cookie = build_cookie()

    print(f"\n[2] 请求作品详情 aweme_id={args.aweme_id} ...")
    try:
        raw = await probe(args.aweme_id, cookie)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 请求异常 {type(exc).__name__}: {exc}")
        return 1

    print("\n[3] 响应解析：")
    dump(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
