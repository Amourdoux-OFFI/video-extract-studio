# -*- coding: utf-8 -*-
"""移动水印工作流验证：逐点取样 -> 时间分段去除 -> 抽帧比对。

B站的防搬运动态水印位置随时间漂移，全片持续性检测抓不到它。
本脚本验证设计中的替代流程：
  1. 在若干时刻分别做单帧检测，得到该时刻的水印框
  2. 给每个框配上生效时间窗
  3. 合成一条滤镜链，一次性去掉整条轨迹上的水印
"""

import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import downloader  # noqa: E402
from app.extractors.registry import resolve_url  # noqa: E402
from app.media import mux, watermark as WM  # noqa: E402

FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
WORK = ROOT / "temp" / "moving"
WORK.mkdir(parents=True, exist_ok=True)

LINK = "https://b23.tv/wD3ZkRM"
# 移动水印在不同时间点出现的位置（秒）
PROBES = [8.0, 40.0, 74.0, 107.0, 125.0]


async def ensure_source() -> Path:
    out = WORK / "src.mp4"
    if out.exists() and out.stat().st_size > 1_000_000:
        return out
    print("[0] 下载 1080P 源片 ...")
    items, errs = await resolve_url(LINK)
    if not items:
        raise SystemExit(f"解析失败: {errs}")
    m = items[0]
    v = m.best_stream()
    a = m.best_audio()
    print(f"    {m.title} | {m.author} | {v.label} {v.width}x{v.height}")
    vp, ap = WORK / "v.m4s", WORK / "a.m4s"
    await downloader.download(v.url, vp, headers=v.headers,
                              backup_urls=v.backup_urls, connections=4)
    if a and not v.is_muxed:
        await downloader.download(a.url, ap, headers=a.headers,
                                  backup_urls=a.backup_urls, connections=4)
        mux.mux(vp, ap, out, duration=m.duration)
        vp.unlink(missing_ok=True)
        ap.unlink(missing_ok=True)
    else:
        vp.replace(out)
    return out


def grab(src: Path, t: float, boxes: list, dest: Path) -> bool:
    vf = ""
    if boxes:
        from app.media import probe
        info = probe.summarize(src)
        vv = info.get("video") or {}
        # -ss 会把输出时间轴重置为 0，因此时间窗要按 t 偏移
        vf = WM.build_vf(boxes, width=int(vv.get("width") or 0),
                         height=int(vv.get("height") or 0),
                         duration=float(info.get("duration") or 0),
                         mode="auto", strength=3, tail="scale=760:-2",
                         time_offset=t)
    else:
        vf = "scale=760:-2"
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error",
           "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
           "-vf", vf, "-y", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=240)
    return dest.exists() and dest.stat().st_size > 0 or bool(print(r.stderr[-200:]))


def main() -> int:
    src = asyncio.run(ensure_source())
    print(f"    源片 {src.stat().st_size/1048576:.1f} MB")

    print("\n[1] 逐点单帧检测（移动水印跟踪靠人工取样）")
    boxes = []
    window = 16.0  # 每个取样点覆盖 ±8 秒
    for t in PROBES:
        r = WM.detect(src, t=t)
        found = r.get("boxes") or []
        print(f"    t={t:6.1f}s  {r['note']}")
        for b in found:
            b["t0"] = round(max(0.0, t - window / 2), 2)
            b["t1"] = round(t + window / 2, 2)
            print(f"         x=%-5d y=%-5d w=%-4d h=%-4d  生效 {b['t0']:.0f}~{b['t1']:.0f}s"
                  % (b["x"], b["y"], b["w"], b["h"]))
            boxes.append(b)

    if not boxes:
        print("    !! 未取到任何框")
        return 1

    print(f"\n[2] 合成滤镜链（{len(boxes)} 段）")
    vf = WM.build_vf(boxes, width=1920, height=1080, duration=134.6,
                     mode="auto", strength=3)
    print(f"    长度 {len(vf)} 字符，段数约 {vf.count('enable=')}")

    print("\n[3] 抽帧比对")
    for t in (8.0, 74.0, 125.0):
        a = WORK / f"before_{int(t)}.png"
        b = WORK / f"after_{int(t)}.png"
        ok1 = grab(src, t, [], a)
        ok2 = grab(src, t, boxes, b)
        print(f"    t={t:6.1f}s  原始={'OK' if ok1 else 'FAIL'}  "
              f"去水印={'OK' if ok2 else 'FAIL'}")

    print(f"\n输出目录: {WORK}")
    print("请比对 before_*.png（应有水印）与 after_*.png（水印应消失）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
