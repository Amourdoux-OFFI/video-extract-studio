# -*- coding: utf-8 -*-
"""B站下载产物水印取证：抓最高画质，按时间轴多点抽帧，供人工判读。"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import downloader  # noqa: E402
from app.extractors.registry import resolve_url  # noqa: E402
from app.media import mux, probe  # noqa: E402

FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
WORK = ROOT / "temp" / "wm"
WORK.mkdir(parents=True, exist_ok=True)

LINKS = [
    ("bili_jiakao", "https://b23.tv/4xjGxJK"),
    ("bili_titanic", "https://b23.tv/wD3ZkRM"),
    ("bili_raokouling", "https://b23.tv/Dmr98rV"),
]


def grab(src: Path, ts: float, dest: Path) -> bool:
    r = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error",
         "-ss", f"{ts:.2f}", "-i", str(src), "-frames:v", "1", "-y", str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return dest.exists() and dest.stat().st_size > 0


async def one(tag: str, url: str) -> dict:
    print(f"\n{'='*74}\n[{tag}] {url}")
    items, errs = await resolve_url(url)
    if not items:
        print("  解析失败:", errs)
        return {}
    m = items[0]
    best = m.best_stream()
    audio = m.best_audio()
    print(f"  {m.title}  |  {m.author}  |  {m.duration}s")
    print(f"  选定: {best.label} {best.width}x{best.height} "
          f"{(best.bandwidth or 0)//1000}kbps {best.codec_family}")
    print(f"  音轨: {audio.label if audio else '无'}")

    vp = WORK / f"{tag}.m4s"
    ap = WORK / f"{tag}.audio.m4s"
    out = WORK / f"{tag}.mp4"
    if not out.exists():
        await downloader.download(best.url, vp, headers=best.headers,
                                  backup_urls=best.backup_urls, connections=4)
        if audio and not best.is_muxed:
            await downloader.download(audio.url, ap, headers=audio.headers,
                                      backup_urls=audio.backup_urls, connections=4)
            mux.mux(vp, ap, out, duration=m.duration)
        else:
            vp.replace(out)
    info = probe.summarize(out)
    print(f"  产物: {info['size_text']}  {info['video']['width']}x{info['video']['height']}")

    dur = float(info["duration"] or 10)
    shots = []
    for frac in (0.08, 0.3, 0.55, 0.8):
        ts = max(0.5, dur * frac)
        p = WORK / f"{tag}_{int(ts)}s.png"
        if grab(out, ts, p):
            shots.append(str(p))
            print(f"    frame {ts:6.1f}s -> {p.name}")
    return {"tag": tag, "title": m.title, "author": m.author,
            "size": info["size"], "w": info["video"]["width"],
            "h": info["video"]["height"], "shots": shots, "video": str(out)}


async def main() -> int:
    res = []
    for tag, url in LINKS:
        res.append(await one(tag, url))
    (WORK / "report.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {WORK/'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
