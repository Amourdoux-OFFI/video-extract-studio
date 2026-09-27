# -*- coding: utf-8 -*-
"""B站 playurl 调试：定位为何拿不到 video 轨。"""

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


async def main() -> int:
    import httpx

    from app.cookies import bilibili_cookie

    cookie = bilibili_cookie()
    print(f"bilibili_cookie 长度 = {len(cookie)}")
    if cookie:
        print("  前 200 字:", cookie[:200].replace("\n", " "))
        names = [p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p]
        print("  字段:", ", ".join(names[:30]))

    bv = "BV1fpY26SERx"
    cid = 41801877928

    variants = {
        "无Cookie": "",
        "带浏览器Cookie": cookie,
    }

    for label, ck in variants.items():
        h = {
            "User-Agent": UA,
            "Referer": f"https://www.bilibili.com/video/{bv}",
            "Origin": "https://www.bilibili.com",
            "Accept": "*/*",
        }
        if ck:
            h["Cookie"] = ck
        print(f"\n========== {label} ==========")
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as c:
            # view
            r = await c.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bv}, headers=h,
            )
            vj = r.json()
            print(f"view code={vj.get('code')} msg={vj.get('message')}")
            data = vj.get("data") or {}
            pages = data.get("pages") or []
            real_cid = pages[0]["cid"] if pages else cid
            print(f"  cid={real_cid} pages={len(pages)} title={(data.get('title') or '')[:30]}")

            # playurl
            r2 = await c.get(
                "https://api.bilibili.com/x/player/playurl",
                params={"bvid": bv, "cid": real_cid, "qn": 127, "fnval": 4048, "fourk": 1},
                headers=h,
            )
            print(f"playurl http={r2.status_code} len={len(r2.text)}")
            try:
                pj = r2.json()
            except Exception as exc:  # noqa: BLE001
                print("  非 JSON:", r2.text[:300])
                continue
            print(f"  code={pj.get('code')} message={pj.get('message')}")
            pd = pj.get("data") or {}
            print(f"  data.keys={list(pd.keys())[:14]}")
            print(f"  quality={pd.get('quality')} format={pd.get('format')}")
            print(f"  accept_quality={pd.get('accept_quality')}")
            dash = pd.get("dash")
            print(f"  dash 存在={bool(dash)}")
            if dash:
                print(f"    dash.video 条数={len(dash.get('video') or [])}")
                print(f"    dash.audio 条数={len(dash.get('audio') or [])}")
                for v in (dash.get("video") or [])[:4]:
                    print(f"      id={v.get('id')} {v.get('width')}x{v.get('height')} "
                          f"bw={v.get('bandwidth')} codecs={v.get('codecs')} "
                          f"baseUrl={'有' if v.get('baseUrl') else '无'}")
            print(f"  durl={len(pd.get('durl') or [])}")
            if not dash:
                print("  原始前 500 字:", json.dumps(pd, ensure_ascii=False)[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
