# -*- coding: utf-8 -*-
"""剪辑编排：把「裁剪 + 压缩」合成一次 ffmpeg 执行，并输出统一 mp4。

核心原则：能不重编码就不重编码。
  - 只裁剪 + 智能/强制无损  ->  -c copy（秒级、零损失）
  - 只裁剪 + 帧精确          ->  仅重编码，CRF 18（视觉无损）
  - 开了压缩                 ->  按所选模式重编码
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from app.core.models import human_size, safe_filename
from app.media import compress as C
from app.media import probe as P
from app.media import runner, trim as T
from app.media import watermark as WM

log = logging.getLogger("vedio.edit")

# 这些视频编码可以直接 copy 进 mp4
MP4_SAFE_V = {"h264", "hevc", "mpeg4", "av1"}
MP4_SAFE_A = {"aac", "mp3", "ac3", "eac3", "alac", "opus"}


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, n = path.stem, path.suffix, 2
    while True:
        cand = path.with_name(f"{stem}({n}){suffix}")
        if not cand.exists():
            return cand
        n += 1


def output_name(src: str | Path, *, suffix: str = "剪辑") -> str:
    p = Path(src)
    return safe_filename(f"{p.stem}_{suffix}") + ".mp4"


def _copy_ok(info: dict) -> bool:
    v = (info.get("video") or {}).get("codec") or ""
    a = (info.get("audio") or {}).get("codec") or ""
    if v and v not in MP4_SAFE_V:
        return False
    if a and a not in MP4_SAFE_A:
        return False
    return True


def build_plan(
    src: str, trim: dict | None, compress: dict | None, watermark: dict | None = None
) -> dict:
    """产出执行计划（不落盘），供 API 的 /estimate 与 /edit 共用。"""
    info = P.summarize(src)
    if not info.get("exists"):
        raise runner.FFmpegError(f"文件不存在：{src}")

    duration = float(info.get("duration") or 0)
    src_size = int(info.get("size") or 0)
    v = info.get("video") or {}
    a = info.get("audio") or {}

    trim = trim or {}
    compress = compress or {}
    watermark = watermark or {}
    do_trim = bool(trim.get("enabled"))
    do_comp = bool(compress.get("enabled"))
    comp_mode = str(compress.get("mode") or "crf").lower()

    # ---- 去水印 ----
    wm_boxes: list[dict] = []
    if watermark.get("enabled"):
        wm_boxes = [b for b in (watermark.get("boxes") or []) if b]
    wm_mode = str(watermark.get("mode") or "auto")
    wm_strength = int(watermark.get("strength") or 3)
    wm_info = WM.summary({"enabled": bool(wm_boxes), "boxes": wm_boxes,
                          "mode": wm_mode, "strength": wm_strength}, duration)

    planned = None
    if do_trim:
        frames: list[float] = []
        if str(trim.get("mode") or "smart").lower() != "precise":
            try:
                frames = P.keyframes(src).get("keyframes") or []
            except Exception as exc:  # noqa: BLE001
                log.warning("关键帧读取失败：%s", exc)
        planned = T.plan(
            float(trim.get("start") or 0),
            float(trim.get("end") or duration),
            str(trim.get("mode") or "smart"),
            frames,
            duration,
        )

    use_nvenc = bool(compress.get("use_nvenc"))
    mute = bool(compress.get("mute"))
    max_height = int(compress.get("max_height") or 0)
    crf = int(compress.get("crf") or 21)
    preset = str(compress.get("preset") or "medium")

    # 决策：是否重编码视频
    trim_mode = (planned or {}).get("mode")
    if not do_comp and (not do_trim or trim_mode == "lossless"):
        video_mode = "copy"
    elif not do_comp and trim_mode == "precise":
        video_mode = "reencode"  # 仅因帧精确
    elif do_comp and comp_mode == "none":
        video_mode = "reencode" if (do_trim and trim_mode == "precise") else "copy"
    else:
        video_mode = "reencode"

    if video_mode == "copy" and not _copy_ok(info):
        video_mode = "reencode"
    # 去水印必须重新编码，无损模式不可用
    if wm_boxes:
        video_mode = "reencode"

    out_dur = duration
    if planned:
        out_dur = max(0.05, float(planned["end"]) - float(planned["start"]))

    est = C.estimate(
        duration=out_dur,
        src_size=int(src_size * (out_dur / duration)) if duration else src_size,
        src_w=int(v.get("width") or 0),
        src_h=int(v.get("height") or 0),
        src_video_bitrate=int(v.get("bitrate") or 0),
        mode=comp_mode if do_comp else "none",
        crf=crf,
        max_height=max_height,
        target_mb=float(compress.get("target_mb") or 0),
        audio_kbps=128,
        fps=float(v.get("fps") or 30.0),
        use_nvenc=use_nvenc,
    )

    return {
        "src": str(src),
        "info": info,
        "duration": duration,
        "out_duration": round(out_dur, 3),
        "trim": planned,
        "do_comp": do_comp,
        "compress_mode": comp_mode if do_comp else "none",
        "video_mode": video_mode,
        "use_nvenc": use_nvenc,
        "mute": mute,
        "max_height": max_height,
        "crf": crf,
        "preset": preset,
        "target_mb": float(compress.get("target_mb") or 0),
        "estimate": est,
        "src_width": int(v.get("width") or 0),
        "src_height": int(v.get("height") or 0),
        "src_video_kbps": int((v.get("bitrate") or 0) // 1000),
        "src_has_audio": bool(a),
        "wm_boxes": wm_boxes,
        "wm_mode": wm_mode,
        "wm_strength": wm_strength,
        "wm_info": wm_info,
    }


def _common_args(plan: dict) -> list[str]:
    args: list[str] = []
    planned = plan.get("trim")
    if planned:
        args += ["-ss", f"{float(planned['start']):.3f}"]
    args += ["-i", plan["src"]]
    if planned:
        dur = max(0.05, float(planned["end"]) - float(planned["start"]))
        args += ["-t", f"{dur:.3f}"]
    return args


def _video_filters(plan: dict) -> str:
    """组装完整视频滤镜链：去水印 + 缩放。"""
    tail = ""
    if plan.get("max_height") and plan.get("src_height") and plan["src_height"] > plan["max_height"]:
        tail = f"scale=-2:{int(plan['max_height'])}"
    if plan.get("wm_boxes"):
        # 裁剪时 -ss 会把输出时间轴重置为 0，水印时间窗必须同步偏移，
        # 否则「裁剪 + 去水印」同时使用时窗口会整体错位。
        offset = float((plan.get("trim") or {}).get("start") or 0.0)
        return WM.build_vf(
            plan["wm_boxes"],
            width=int(plan.get("src_width") or 0),
            height=int(plan.get("src_height") or 0),
            duration=float(plan.get("duration") or 0),
            mode=plan.get("wm_mode") or "auto",
            strength=int(plan.get("wm_strength") or 3),
            tail=tail,
            time_offset=offset,
        )
    return tail


def _video_args(plan: dict, *, target_kbps: int = 0) -> list[str]:
    if plan["video_mode"] == "copy":
        return ["-c:v", "copy"]
    # CRF 模式下把码率钳在源码率之内，保证「压缩后不会反而变大」
    cap = 0
    if target_kbps <= 0 and plan.get("do_comp") and plan.get("compress_mode") == "crf":
        cap = int(plan.get("src_video_kbps") or 0)
    args = C.video_encode_args(
        crf=plan["crf"],
        preset=plan["preset"],
        use_nvenc=plan["use_nvenc"],
        target_kbps=target_kbps,
        max_kbps=cap,
    )
    vf = _video_filters(plan)
    if vf:
        args += ["-vf", vf]
    return args


def _audio_args(plan: dict, *, kbps: int = 128) -> list[str]:
    if plan["mute"]:
        return ["-an"]
    if not plan["src_has_audio"]:
        return ["-an"]
    if plan["video_mode"] == "copy" and not plan["do_comp"]:
        return ["-c:a", "copy"]
    return C.audio_encode_args(kbps=kbps, mute=False)


def run_edit(
    src: str,
    out_dir: str | Path,
    *,
    trim: dict | None = None,
    compress: dict | None = None,
    watermark: dict | None = None,
    on_progress=None,
    cancel=None,
) -> dict:
    """执行剪辑，返回 {path, size, size_text, plan, seconds}。"""
    plan = build_plan(src, trim, compress, watermark)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tag = []
    if plan["trim"]:
        tag.append("裁剪")
    if plan["do_comp"] and plan["compress_mode"] != "none":
        tag.append(f"CRF{plan['crf']}" if plan["compress_mode"] == "crf" else "目标体积")
    if plan.get("wm_info", {}).get("enabled"):
        tag.append("去水印")
    if not tag:
        tag.append("转封装")
    out_path = _unique(out_dir / output_name(src, suffix="_".join(tag)))

    total = float(plan["out_duration"] or plan["duration"] or 0)
    is_target = plan["compress_mode"] == "target" and plan["target_mb"] > 0
    v_kbps, a_kbps = (0, 128)
    if is_target:
        v_kbps, a_kbps = C.target_bitrates(plan["target_mb"], total, 128)

    def phase_cb(lo: float, hi: float, label: str):
        if on_progress is None:
            return None

        def cb(pct: float, speed: float, eta: float):
            try:
                on_progress(lo + (hi - lo) * (pct / 100.0), label, speed)
            except Exception:  # noqa: BLE001
                pass

        return cb

    # ---------- 两遍编码（目标体积） ----------
    if is_target and plan["video_mode"] != "copy":
        prefix = out_dir / f".pass_{out_path.stem}"
        args1 = _common_args(plan) + _video_args(plan, target_kbps=v_kbps)
        args1 += ["-pass", "1", "-passlogfile", str(prefix), "-an", "-f", "null", "-"]
        r1 = runner.run(
            args1,
            duration=total,
            on_progress=phase_cb(0.0, 50.0, "压缩 第1遍（分析）"),
            cancel=cancel,
        )
        if not r1.ok:
            _cleanup_passlog(prefix)
            raise runner.FFmpegError(f"第一遍编码失败：{r1.stderr[-400:]}")

        args2 = _common_args(plan) + _video_args(plan, target_kbps=v_kbps)
        args2 += ["-pass", "2", "-passlogfile", str(prefix)]
        args2 += _audio_args(plan, kbps=a_kbps)
        args2 += ["-movflags", "+faststart", str(out_path)]
        r2 = runner.run(
            args2,
            duration=total,
            on_progress=phase_cb(50.0, 100.0, "压缩 第2遍（输出）"),
            cancel=cancel,
        )
        _cleanup_passlog(prefix)
        if not r2.ok:
            raise runner.FFmpegError(f"第二遍编码失败：{r2.stderr[-400:]}")
        seconds = r1.seconds + r2.seconds
    else:
        # ---------- 单遍 ----------
        args = _common_args(plan) + _video_args(plan) + _audio_args(plan)
        args += ["-movflags", "+faststart", str(out_path)]
        label = (
            "无损裁剪"
            if plan["video_mode"] == "copy" and plan["trim"]
            else ("转封装" if plan["video_mode"] == "copy" else "压缩编码")
        )
        r = runner.run(
            args,
            duration=total,
            on_progress=phase_cb(0.0, 100.0, label),
            cancel=cancel,
        )
        if r.canceled:
            out_path.unlink(missing_ok=True)
            raise runner.Canceled("已取消")
        if not r.ok:
            raise runner.FFmpegError(f"处理失败：{r.stderr[-400:]}")
        seconds = r.seconds

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise runner.FFmpegError("输出文件为空")

    size = out_path.stat().st_size
    src_size = int(plan["info"].get("size") or 0)
    return {
        "path": str(out_path),
        "name": out_path.name,
        "size": size,
        "size_text": human_size(size),
        "src_size": src_size,
        "src_size_text": human_size(src_size),
        "saved_text": (
            f"{(1 - size / src_size) * 100:.1f}%"
            if src_size and size < src_size
            else ("增大" if src_size else "")
        ),
        "seconds": round(seconds, 2),
        "plan": {
            "video_mode": plan["video_mode"],
            "trim": plan["trim"],
            "compress_mode": plan["compress_mode"],
            "crf": plan["crf"],
            "use_nvenc": plan["use_nvenc"],
            "out_duration": plan["out_duration"],
            "watermark": plan.get("wm_info") or {"enabled": False},
        },
    }


def _cleanup_passlog(prefix: Path) -> None:
    parent = prefix.parent
    stem = prefix.name
    try:
        for f in parent.glob(f"{stem}*"):
            if f.suffix in (".log", ".mbtree") or f.name.endswith(".log.mbtree"):
                f.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
