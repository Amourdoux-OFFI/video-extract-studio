# -*- coding: utf-8 -*-
"""把各码流同一时间点的同一角落裁出来拼成一张图，并计算与参考帧的差异。

判定逻辑：先按比例裁出左上角/右上角，再统一缩放到同一尺寸。
有投稿水印的帧彼此高度相似；若某条码流没有水印，它与其余帧的差异会显著更大。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
FFPROBE = ROOT / "vendor" / "ffmpeg" / "ffprobe.exe"
SRC = ROOT / "temp" / "wm2"
OUT = ROOT / "temp" / "wm2_cmp"
OUT.mkdir(parents=True, exist_ok=True)

CW, CH = 520, 120  # 裁出并缩放后的角落尺寸


def run(args):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def crop_corner(src: Path, dest: Path, corner: str) -> bool:
    """按比例裁角落：宽 32%、高 14%。"""
    x = "0" if corner == "tl" else "iw*0.68"
    vf = f"crop=iw*0.32:ih*0.14:{x}:0,scale={CW}:{CH}"
    r = run([str(FFMPEG), "-hide_banner", "-loglevel", "error",
             "-i", str(src), "-vf", vf, "-frames:v", "1", "-y", str(dest)])
    return dest.exists() and dest.stat().st_size > 0


def mean_abs_diff(a: Path, b: Path) -> float:
    """用 ffmpeg 的 blend=difference + signalstats 求平均绝对差。"""
    r = run([str(FFMPEG), "-hide_banner", "-loglevel", "info",
             "-i", str(a), "-i", str(b),
             "-filter_complex",
             "[0][1]blend=all_mode=difference,signalstats,"
             "metadata=print:key=lavfi.signalstats.YAVG",
             "-f", "null", "-"])
    for line in (r.stderr or "").splitlines():
        if "YAVG=" in line:
            try:
                return float(line.split("YAVG=")[1].strip())
            except ValueError:
                pass
    return -1.0


def tile(images: list[Path], dest: Path, cols: int = 2) -> bool:
    n = len(images)
    if not n:
        return False
    rows = (n + cols - 1) // cols
    args = [str(FFMPEG), "-hide_banner", "-loglevel", "error"]
    for p in images:
        args += ["-i", str(p)]
    parts = "".join(f"[{i}]" for i in range(n))
    if n == 1:
        fc = "[0]null[out]"
    else:
        fc = f"{parts}xstack=inputs={n}:fill=black:layout=" + "|".join(
            f"{(i % cols) * CW}_{(i // cols) * CH}" for i in range(n)
        ) + "[out]"
    args += ["-filter_complex", fc, "-map", "[out]", "-y", str(dest)]
    r = run(args)
    return dest.exists() and dest.stat().st_size > 0


def main() -> int:
    imgs = sorted(p for p in SRC.glob("*.png") if p.name[:2].isdigit())
    if not imgs:
        print("!! 没有帧文件，请先运行 probe_bili_allstreams.py")
        return 1

    for corner, label in (("tl", "左上角"), ("tr", "右上角")):
        crops = []
        for p in imgs:
            d = OUT / f"{corner}_{p.name}"
            if crop_corner(p, d, corner):
                crops.append(d)
        if not crops:
            continue
        sheet = OUT / f"sheet_{corner}.png"
        ok = tile(crops, sheet)
        print(f"\n=== {label} 拼图（{len(crops)} 张，2 列）{'OK' if ok else 'FAIL'} ===")
        print(f"    {sheet}")
        print("    顺序（自上而下、自左而右）：")
        for i, p in enumerate(crops):
            print(f"      {i+1:2d}. {p.name[3:-4]}")

        # 数值差异：以第 1 张为参考
        ref = crops[0]
        print(f"\n    与参考帧({ref.name[3:-4]})的平均绝对差（越大越可能没水印）：")
        for p in crops:
            d = mean_abs_diff(ref, p)
            flag = "  <== 差异明显" if d > 6.0 else ""
            print(f"      {p.name[3:-4]:<34} {d:6.2f}{flag}")

    print(f"\n输出目录: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
