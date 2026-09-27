# -*- coding: utf-8 -*-
"""剪辑 / 压缩 端到端自测：无损裁剪、帧精确裁剪、CRF、目标体积、NVENC、本地导入。"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.media import edit as editor  # noqa: E402
from app.media import probe, trim as T  # noqa: E402


def line(t=""):
    print(t, flush=True)


def pick_source() -> Path | None:
    out = ROOT / "output"
    files = [p for p in out.glob("*.mp4") if p.stat().st_size > 500_000]
    if not files:
        return None
    # 选时长最长的一条，便于裁剪测试
    best, dur = None, -1
    for p in files:
        d = probe.summarize(p).get("duration") or 0
        if d > dur:
            best, dur = p, d
    return best


def run_case(name: str, src: Path, **kw) -> dict:
    trim = kw.get("trim")
    comp = kw.get("compress")
    t0 = time.time()
    plan = editor.build_plan(str(src), trim, comp)
    est = plan.get("estimate") or {}
    line(f"\n--- {name}")
    line(f"    决策: video_mode={plan['video_mode']}  输出时长={plan['out_duration']}s")
    if plan.get("trim"):
        line(f"    裁剪: {plan['trim']['mode']} {plan['trim']['start']}s -> "
             f"{plan['trim']['end']}s 吸附={plan['trim']['snapped']}")
        line(f"          {plan['trim']['note']}")
    if est.get("ok"):
        line(f"    预估: {est.get('src_size',0)/1048576:.2f}MB -> "
             f"{est.get('est_size',0)/1048576:.2f}MB  ({est.get('note','')[:60]})")

    try:
        res = editor.run_edit(str(src), ROOT / "output" / "剪辑测试",
                              trim=trim, compress=comp)
    except Exception as exc:  # noqa: BLE001
        line(f"    ✘ 失败 {type(exc).__name__}: {exc}")
        return {"ok": False, "name": name, "error": str(exc)}

    dt = time.time() - t0
    info = probe.summarize(res["path"])
    line(f"    ✔ {res['name']}")
    line(f"      {res['size_text']}  (源 {res['src_size_text']}, 变化 {res['saved_text']})  "
         f"耗时 {dt:.1f}s  实际时长={info.get('duration')}s")
    v = info.get("video") or {}
    a = info.get("audio") or {}
    line(f"      视频 {v.get('codec')} {v.get('width')}x{v.get('height')}  音频 {a.get('codec') or '无'}")

    ok = True
    problems = []
    if not Path(res["path"]).exists():
        ok, problems = False, ["输出文件不存在"]
    if plan["out_duration"] and info.get("duration"):
        delta = abs(info["duration"] - plan["out_duration"])
        if delta > 1.5:
            ok = False
            problems.append(f"时长偏差 {delta:.2f}s")
    if not v.get("codec"):
        ok, problems = False, problems + ["无视频流"]
    for p in problems:
        line(f"      ✘ {p}")
    return {"ok": ok, "name": name, "size": res["size"], "seconds": dt,
            "path": res["path"], "duration": info.get("duration"),
            "error": "; ".join(problems)}


def main() -> int:
    src = pick_source()
    if not src:
        line("!! output/ 下没有可用源文件，请先跑 tools/test_api.py 下载一条")
        return 1
    info = probe.summarize(src)
    line("=" * 78)
    line(f"源文件: {src.name}")
    line(f"  {info['size_text']}  {info['duration']}s  "
         f"{info['video']['codec']} {info['video']['width']}x{info['video']['height']} "
         f"{info['video']['fps']}fps  码率 {info['bitrate']//1000} kbps")

    # 关键帧
    kf = probe.keyframes(src)
    line(f"  关键帧: {len(kf['keyframes'])} 个，前 8 个 = {kf['keyframes'][:8]}")

    results = []
    dur = float(info["duration"] or 30)
    s, e = 5.0, min(dur - 1, 20.0)
    if e - s < 3:
        s, e = 0.5, max(2.0, dur * 0.6)

    # 1 智能无损裁剪
    results.append(run_case(
        "1 智能裁剪（无损优先 + 关键帧吸附）", src,
        trim={"enabled": True, "start": s, "end": e, "mode": "smart"},
        compress={"enabled": False},
    ))
    # 2 帧精确裁剪
    results.append(run_case(
        "2 帧精确裁剪（重编码，CRF18）", src,
        trim={"enabled": True, "start": s + 0.37, "end": e - 0.21, "mode": "precise"},
        compress={"enabled": False},
    ))
    # 3 CRF 压缩（x264）
    results.append(run_case(
        "3 CRF21 压缩（x264 medium）", src,
        trim={"enabled": False},
        compress={"enabled": True, "mode": "crf", "crf": 21, "preset": "medium",
                  "max_height": 0, "use_nvenc": False},
    ))
    # 4 CRF + 限制 480P
    results.append(run_case(
        "4 CRF24 压缩 + 限制到 480P", src,
        trim={"enabled": False},
        compress={"enabled": True, "mode": "crf", "crf": 24, "preset": "fast",
                  "max_height": 480, "use_nvenc": False},
    ))
    # 5 NVENC 硬件加速
    results.append(run_case(
        "5 NVENC 硬件加速压缩（CRF21->cq23）", src,
        trim={"enabled": False},
        compress={"enabled": True, "mode": "crf", "crf": 21, "preset": "p5",
                  "max_height": 0, "use_nvenc": True},
    ))
    # 6 目标体积（两遍）
    results.append(run_case(
        "6 目标体积 1MB（两遍 ABR）+ 裁剪", src,
        trim={"enabled": True, "start": s, "end": e, "mode": "smart"},
        compress={"enabled": True, "mode": "target", "target_mb": 1.0,
                  "preset": "medium", "use_nvenc": False},
    ))
    # 7 静音
    results.append(run_case(
        "7 仅裁剪并静音", src,
        trim={"enabled": True, "start": s, "end": e, "mode": "lossless"},
        compress={"enabled": True, "mode": "none", "mute": True},
    ))

    line("\n" + "=" * 78)
    line("汇总")
    bad = [r for r in results if not r.get("ok")]
    for r in results:
        mark = "✔" if r.get("ok") else "✘"
        line(f"  {mark} {r['name']:<40} {r.get('seconds',0):6.1f}s  "
             f"{r.get('error','')}")
    line(f"\n  {len(results)-len(bad)}/{len(results)} 项通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
