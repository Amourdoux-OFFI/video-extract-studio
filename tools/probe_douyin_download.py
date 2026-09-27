# -*- coding: utf-8 -*-
"""抖音无水印判定实测。

对同一个作品分别下载：
  A. video.bit_rate[] 中 format=mp4 且码率最高的一档
  B. video.download_addr （业界公认的"带水印"地址）
抽出同一时间点的帧，交由人工/图像比对判定哪一路无水印。

用法:
    python tools/probe_douyin_download.py <aweme_id>
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMP = ROOT / "temp"
TEMP.mkdir(parents=True, exist_ok=True)
FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
FFPROBE = ROOT / "vendor" / "ffmpeg" / "ffprobe.exe"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def build_cookie() -> str:
    from f2.apps.douyin.utils import TokenManager, VerifyFpManager

    parts = [f"msToken={TokenManager.gen_real_msToken()}"]
    for name, fn in (
        ("ttwid", TokenManager.gen_ttwid),
        ("webid", TokenManager.gen_webid),
        ("s_v_web_id", VerifyFpManager.gen_s_v_web_id),
    ):
        try:
            parts.append(f"{name}={fn()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] {name}: {exc}")
    return "; ".join(parts)


async def fetch_detail(aweme_id: str) -> dict:
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import PostDetail

    kwargs = {
        "cookie": build_cookie(),
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
        return await crawler.fetch_post_detail(PostDetail(aweme_id=aweme_id))


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            str(FFPROBE), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        j = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    fmt = j.get("format", {})
    info = {
        "size": int(fmt.get("size") or 0),
        "duration": float(fmt.get("duration") or 0),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "streams": [],
    }
    for s in j.get("streams", []):
        info["streams"].append(
            {
                "type": s.get("codec_type"),
                "codec": s.get("codec_name"),
                "w": s.get("width"),
                "h": s.get("height"),
                "fps": s.get("r_frame_rate"),
                "br": s.get("bit_rate"),
            }
        )
    return info


def download(url: str, dest: Path, referer: str | None, use_ua: bool = True) -> tuple[int, int]:
    import httpx

    headers = {}
    if use_ua:
        headers["User-Agent"] = UA
    if referer:
        headers["Referer"] = referer
    t0 = time.time()
    total = 0
    with httpx.Client(follow_redirects=True, timeout=60.0, headers=headers) as c:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_bytes(1 << 18):
                    f.write(chunk)
                    total += len(chunk)
    return r.status_code, total


def grab_frame(src: Path, ts: float, dest: Path) -> bool:
    out = subprocess.run(
        [
            str(FFMPEG), "-hide_banner", "-loglevel", "error",
            "-ss", f"{ts:.2f}", "-i", str(src),
            "-frames:v", "1", "-y", str(dest),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return dest.exists() and dest.stat().st_size > 0 or (print(out.stderr[-300:]) or False)


def main() -> int:
    aweme_id = sys.argv[1]
    raw = asyncio.run(fetch_detail(aweme_id))
    detail = raw.get("aweme_detail") or {}
    if not detail:
        print("!! 未取到 aweme_detail")
        return 1

    video = detail.get("video") or {}
    print(f"标题        = {(detail.get('desc') or '')[:50]}")
    print(f"作者        = {(detail.get('author') or {}).get('nickname')}")
    print(f"时长        = {detail.get('duration')} ms")
    print(f"has_watermark = {video.get('has_watermark')}")
    print(f"cdn_url_expired = {video.get('cdn_url_expired')}")
    print(f"is_h265     = {video.get('is_h265')}")

    play = (video.get("play_addr") or {}).get("url_list") or []
    dl = (video.get("download_addr") or {}).get("url_list") or []
    print(f"\n顶层 play_addr   ({len(play)}): {play[0][:110] if play else '-'}")
    print(f"download_addr    ({len(dl)}): {dl[0][:110] if dl else '-'}")
    print(f"download_suffix_logo_addr = {bool(video.get('download_suffix_logo_addr'))}")
    print(f"has_download_suffix_logo_addr = {video.get('has_download_suffix_logo_addr')}")

    br = video.get("bit_rate") or []
    mp4s = [b for b in br if b.get("format") == "mp4"]
    dashes = [b for b in br if b.get("format") == "dash"]
    print(f"\nbit_rate 档位: 共 {len(br)}  (mp4={len(mp4s)}, dash={len(dashes)})")
    for b in sorted(mp4s, key=lambda x: x.get("bit_rate") or 0, reverse=True)[:6]:
        print(f"  mp4  {b.get('gear_name'):<18} bw={b.get('bit_rate'):<8} qn={b.get('quality_type')}")

    if not mp4s:
        print("!! 没有 mp4 档位")
        return 1

    best = max(mp4s, key=lambda x: x.get("bit_rate") or 0)
    best_url = (best.get("play_addr") or {}).get("url_list", [None])[0]
    print(f"\n最高档 = {best.get('gear_name')} bw={best.get('bit_rate')}")

    referer = "https://www.douyin.com/"
    out_a = TEMP / "dy_nobar.mp4"
    out_b = TEMP / "dy_downloadaddr.mp4"

    print("\n[A] 下载 bit_rate 最高档 ...")
    try:
        code, n = download(best_url, out_a, referer)
        print(f"    http={code} bytes={n} ({n/1048576:.2f} MB)")
    except Exception as exc:  # noqa: BLE001
        print(f"    !! 失败 {type(exc).__name__}: {exc}")
        out_a = None

    if dl:
        print("[B] 下载 download_addr ...")
        try:
            code, n = download(dl[0], out_b, referer)
            print(f"    http={code} bytes={n} ({n/1048576:.2f} MB)")
        except Exception as exc:  # noqa: BLE001
            print(f"    !! 失败 {type(exc).__name__}: {exc}")
            out_b = None

    print("\n[C] 无 Referer 测试 ...")
    try:
        code, n = download(best_url, TEMP / "dy_noref.mp4", None)
        print(f"    无 Referer -> http={code} bytes={n}")
    except Exception as exc:  # noqa: BLE001
        print(f"    无 Referer -> 失败 {type(exc).__name__}: {exc}")

    for label, p in (("A 最高档", out_a), ("B download_addr", out_b)):
        if p and p.exists():
            info = probe(p)
            print(f"\n[{label}] {p.name}")
            print(f"  size={info.get('size',0)/1048576:.2f}MB dur={info.get('duration')} "
                  f"bitrate={info.get('bit_rate')}")
            for s in info.get("streams", []):
                print(f"  stream {s['type']:<6} {s['codec']:<8} {s['w']}x{s['h']} "
                      f"fps={s['fps']} br={s['br']}")
            ts = max(1.0, (info.get("duration") or 10) * 0.45)
            frame = TEMP / f"frame_{p.stem}.png"
            if grab_frame(p, ts, frame):
                print(f"  抽帧 -> {frame}  ({frame.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
