# -*- coding: utf-8 -*-
"""水印检测与去除的效果验证：检出框 -> 画框标注图 -> 去水印对比图。"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.media import watermark as WM  # noqa: E402

FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
OUT = ROOT / "temp" / "wmtest"
OUT.mkdir(parents=True, exist_ok=True)

TARGET = ROOT / "temp" / "wm" / "bili_titanic.mp4"   # 有固定水印 + 移动水印
TIMES = (10.0, 74.0)


def grab(path: Path, t: float, vf: str, dest: Path) -> tuple[bool, str]:
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error",
           "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-y", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    ok = dest.exists() and dest.stat().st_size > 0
    return ok, (r.stderr or "").strip()[-300:]


def main() -> int:
    src = Path(TARGET)
    if not src.exists():
        print(f"!! 找不到测试文件 {src}")
        return 1
    print(f"测试文件: {src.name}  ({src.stat().st_size/1048576:.1f} MB)")

    print("\n[1] 检测水印 ...")
    r = WM.detect(src, samples=48)
    print(f"    ok={r.get('ok')} 耗时={r.get('seconds')}s  动画={r.get('animated')}")
    print(f"    画面 {r.get('width')}x{r.get('height')}  时长 {r.get('duration')}s")
    print(f"    说明: {r.get('note')}")
    print(f"    固定框 ({len(r.get('static_boxes') or [])}):")
    for b in r.get("static_boxes") or []:
        print(f"      x={b['x']:5d} y={b['y']:5d} w={b['w']:4d} h={b['h']:4d}")
    segs = r.get("segments") or []
    print(f"    限时框 ({len(segs)}):")
    for b in segs[:8]:
        print(f"      x={b['x']:5d} y={b['y']:5d} w={b['w']:4d} h={b['h']:4d} "
              f"t={b['t0']:.1f}~{b['t1']:.1f}")
    if len(segs) > 8:
        print(f"      ... 共 {len(segs)} 个")
    for tr in r.get("tracks") or []:
        print(f"    轨迹: static={tr['static']} samples={tr['samples']}")

    if not r.get("ok"):
        return 1

    boxes = r.get("boxes") or []
    W, H, DUR = r["width"], r["height"], r["duration"]

    # ---- 标注图：把检出的框画出来 ----
    print("\n[2] 生成标注图 ...")
    for t in TIMES:
        active = [
            b for b in boxes
            if (float(b.get("t0") or 0) <= t <= (float(b.get("t1") or 0) or DUR))
        ]
        vf = ",".join(
            f"drawbox=x={b['x']}:y={b['y']}:w={b['w']}:h={b['h']}:color=red@0.9:t=4"
            for b in active
        ) or "null"
        d = OUT / f"marked_{int(t)}s.png"
        ok, err = grab(src, t, vf, d)
        print(f"    {t:5.1f}s 生效框 {len(active):2d} 个 -> {'OK' if ok else 'FAIL ' + err}")

    # ---- 去水印对比 ----
    print("\n[3] 生成去水印对比图 ...")
    for mode in ("auto", "blur", "mosaic"):
        for t in TIMES:
            vf = WM.build_vf(boxes, width=W, height=H, duration=DUR,
                             mode=mode, strength=3)
            d = OUT / f"clean_{mode}_{int(t)}s.png"
            ok, err = grab(src, t, vf, d)
            print(f"    mode={mode:<7} {t:5.1f}s -> {'OK' if ok else 'FAIL'}")
            if not ok and err:
                print(f"        vf = {vf[:220]}")
                print(f"        err= {err[:220]}")

    # ---- 原始对照 ----
    for t in TIMES:
        grab(src, t, "", OUT / f"orig_{int(t)}s.png")

    print(f"\n输出目录: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
