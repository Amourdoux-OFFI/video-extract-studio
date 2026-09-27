# -*- coding: utf-8 -*-
"""B站全码流取证：把所有可拿到的码流各抓一帧，判断哪一条没有叠加投稿水印。

思路：水印是 B站转码时叠加的，不是源片自带。不同清晰度 / 不同接口
可能是不同的转码产物，因此存在「某条码流干净」的可能。
用 ffmpeg 直接从远端 URL 抓帧（走 Range，不用整包下载），速度快。
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
OUT = ROOT / "temp" / "wm2"
OUT.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BV = "BV1fpY26SERx"          # 驾考宝典（水印在左上角，取帧时间 7s）
CID = 41801877928
TS = 7.0


def grab(url: str, dest: Path, ts: float = TS, referer: str | None = None) -> tuple[bool, str]:
    if dest.exists():
        dest.unlink()
    hdr = f"Referer: {referer or f'https://www.bilibili.com/video/{BV}'}\r\n"
    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
        "-headers", hdr, "-user_agent", UA,
        "-ss", f"{ts:.2f}", "-i", url,
        "-frames:v", "1", "-y", str(dest),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    ok = dest.exists() and dest.stat().st_size > 0
    return ok, (r.stderr or "")[-200:]


async def collect() -> list[dict]:
    """收集各种接口姿势下的候选码流。"""
    cands: list[dict] = []
    ref = f"https://www.bilibili.com/video/{BV}"
    base_h = {"User-Agent": UA, "Referer": ref, "Origin": "https://www.bilibili.com"}

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0, headers=base_h) as c:
        # 1) DASH：所有清晰度 × 所有编码
        r = await c.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": BV, "cid": CID, "qn": 127, "fnval": 4048,
                    "fnver": 0, "fourk": 1, "try_look": 1},
        )
        dash = ((r.json().get("data") or {}).get("dash") or {})
        for v in dash.get("video") or []:
            cands.append({
                "tag": f"dash_q{v.get('id')}_{v.get('codecs','').split('.')[0]}_c{v.get('codecid')}",
                "url": v.get("baseUrl") or v.get("base_url"),
                "res": f"{v.get('width')}x{v.get('height')}",
                "bw": v.get("bandwidth"),
                "api": "playurl fnval=4048 try_look",
            })

        # 2) durl（fnval=1，单文件 FLV/MP4）
        r2 = await c.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": BV, "cid": CID, "qn": 127, "fnval": 1, "fourk": 1, "try_look": 1},
        )
        d2 = r2.json().get("data") or {}
        for d in d2.get("durl") or []:
            cands.append({
                "tag": f"durl_q{d2.get('quality')}_{d2.get('format')}",
                "url": d.get("url"), "res": d2.get("accept_description"),
                "bw": None, "api": "playurl fnval=1",
            })

        # 3) platform=html5
        r3 = await c.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": BV, "cid": CID, "qn": 80, "fnval": 1,
                    "platform": "html5", "high_quality": 1},
        )
        d3 = r3.json().get("data") or {}
        for d in d3.get("durl") or []:
            cands.append({
                "tag": f"html5_q{d3.get('quality')}_{d3.get('format')}",
                "url": d.get("url"), "res": d3.get("accept_description"),
                "bw": None, "api": "playurl platform=html5",
            })

        # 4) TV 端接口（不需要 access_key 时可用）
        try:
            r4 = await c.get(
                "https://api.snm0516.aisee.tv/x/tv/playurl",
                params={"bvid": BV, "cid": CID, "qn": 127, "fnval": 4048,
                        "fourk": 1, "platform": "android", "device": "android"},
            )
            j4 = r4.json()
            d4 = j4.get("data") or {}
            for v in ((d4.get("dash") or {}).get("video") or []):
                cands.append({
                    "tag": f"tv_q{v.get('id')}_{v.get('codecs','').split('.')[0]}",
                    "url": v.get("baseUrl") or v.get("base_url"),
                    "res": f"{v.get('width')}x{v.get('height')}",
                    "bw": v.get("bandwidth"), "api": "TV api.snm0516",
                })
            if not d4:
                print(f"  TV 接口无数据: code={j4.get('code')} {j4.get('message')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  TV 接口失败: {type(exc).__name__}: {exc}")

    return cands


async def main() -> int:
    print("收集候选码流 ...")
    cands = await collect()
    print(f"共 {len(cands)} 条码流\n")

    rows = []
    for i, c in enumerate(cands, 1):
        if not c.get("url"):
            continue
        dest = OUT / f"{i:02d}_{c['tag']}.png"
        ok, err = grab(c["url"], dest)
        status = "OK " if ok else "FAIL"
        print(f"  [{i:02d}] {status} {c['tag']:<34} {c.get('res')} "
              f"bw={c.get('bw')}  ({c.get('api')})")
        if not ok and err:
            print(f"        {err.strip()[:120]}")
        rows.append({**c, "shot": str(dest) if ok else "", "ok": ok})

    print(f"\n帧文件目录: {OUT}")
    print("\n请人工比对每张图的左上角/右上角是否有 'bilibili' 字样与昵称。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
