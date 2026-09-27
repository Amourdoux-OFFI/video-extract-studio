# -*- coding: utf-8 -*-
"""B站 playurl 参数变体对比：找出能拿到最高画质的请求姿势。"""

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BV = "BV1fpY26SERx"
CID = 41801877928


def hdrs(referer, cookie="", extra=None):
    h = {
        "User-Agent": UA,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        h["Cookie"] = cookie
    if extra:
        h.update(extra)
    return h


def report(label, j, cookie_note=""):
    code = j.get("code")
    data = j.get("data") or {}
    dash = data.get("dash") or {}
    vids = sorted({int(v.get("id") or 0) for v in (dash.get("video") or [])}, reverse=True)
    res = sorted(
        {(int(v.get("width") or 0), int(v.get("height") or 0)) for v in (dash.get("video") or [])},
        key=lambda x: -x[1],
    )
    auds = sorted({int(a.get("id") or 0) for a in (dash.get("audio") or [])}, reverse=True)
    flag = "★" if len(vids) > 2 else " "
    print(f" {flag} {label:<34} code={code:<6} 画质id={vids} 分辨率={res} 音轨={auds} {cookie_note}")
    if code != 0:
        print(f"       message={j.get('message')}")


async def main() -> int:
    from app.extractors.bilibili import enc_wbi

    ref = f"https://www.bilibili.com/video/{BV}"
    base = {"bvid": BV, "cid": CID, "qn": 127, "fnval": 4048, "fourk": 1}

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as c:
        # 取 buvid（短链跳转里带）
        r = await c.get("https://b23.tv/4xjGxJK", headers=hdrs("https://www.bilibili.com/"))
        final = str(r.url)
        buvid = ""
        if "buvid=" in final:
            buvid = final.split("buvid=", 1)[1].split("&", 1)[0]
        print(f"短链跳转得到的 buvid = {buvid[:40]}")
        buvid_cookie = f"buvid3={buvid}-infoc" if buvid else ""

        # WBI 密钥
        nav = await c.get(
            "https://api.bilibili.com/x/web-interface/nav", headers=hdrs("https://www.bilibili.com/")
        )
        wbi = ((nav.json() or {}).get("data") or {}).get("wbi_img") or {}
        img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        print(f"WBI keys: img={img[:16]}... sub={sub[:16]}...")
        print()

        U = "https://api.bilibili.com/x/player/playurl"
        W = "https://api.bilibili.com/x/player/wbi/playurl"

        print("=== 各变体结果 ===")

        tests = [
            ("1 免签名 原始", U, dict(base), ""),
            ("2 免签名 +try_look+fnver", U, {**base, "try_look": 1, "fnver": 0}, ""),
            ("3 免签名 +buvid3", U, dict(base), buvid_cookie),
            ("4 免签名 +try_look+buvid3", U, {**base, "try_look": 1, "fnver": 0}, buvid_cookie),
            ("5 WBI 原始", W, enc_wbi(dict(base), img, sub), ""),
            ("6 WBI +try_look+fnver", W, enc_wbi({**base, "try_look": 1, "fnver": 0}, img, sub), ""),
            ("7 WBI +buvid3", W, enc_wbi(dict(base), img, sub), buvid_cookie),
            ("8 WBI +try_look+buvid3", W, enc_wbi({**base, "try_look": 1, "fnver": 0}, img, sub), buvid_cookie),
            ("9 WBI qn=80", W, enc_wbi({**base, "qn": 80}, img, sub), ""),
            ("10 WBI +platform=pc", W, enc_wbi({**base, "platform": "pc", "fnver": 0}, img, sub), ""),
        ]

        for label, api, params, ck in tests:
            try:
                rr = await c.get(api, params=params, headers=hdrs(ref, ck))
                report(label, rr.json(), f"cookie={'有' if ck else '无'}")
            except Exception as exc:  # noqa: BLE001
                print(f"   {label:<34} 异常 {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
