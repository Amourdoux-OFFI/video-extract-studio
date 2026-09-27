# -*- coding: utf-8 -*-
"""裁剪：关键帧吸附 + 三种模式（智能 / 强制无损 / 帧精确）。"""

from __future__ import annotations

import logging

from app.media.compress import video_encode_args

log = logging.getLogger("vedio.trim")

SNAP_TOLERANCE = 1.5  # 秒：误差在此范围内才吸附到关键帧

MODE_LABELS = {
    "smart": "智能无损",
    "lossless": "强制无损",
    "precise": "帧精确",
}


def nearest_keyframe(t: float, frames: list[float]) -> tuple[float, float]:
    """返回 (最近关键帧时间, 误差秒数)。frames 需升序。"""
    if not frames:
        return t, 0.0
    lo, hi = 0, len(frames) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if frames[mid] < t:
            lo = mid + 1
        else:
            hi = mid
    best = frames[lo]
    if lo > 0 and abs(frames[lo - 1] - t) <= abs(best - t):
        best = frames[lo - 1]
    return best, abs(best - t)


def plan(
    start: float,
    end: float,
    mode: str,
    frames: list[float] | None,
    duration: float = 0.0,
) -> dict:
    """决定实际裁剪区间与将采用的模式。

    返回 {mode, start, end, snapped, error, note}
    """
    start = max(0.0, float(start or 0.0))
    end = float(end or 0.0)
    if duration > 0:
        start = min(start, max(0.0, duration - 0.05))
        end = min(end, duration) if end > 0 else duration
    if end <= start:
        end = min(duration, start + 0.1) if duration else start + 0.1

    mode = (mode or "smart").lower()
    frames = frames or []

    if mode == "precise":
        return {
            "mode": "precise",
            "start": round(start, 3),
            "end": round(end, 3),
            "snapped": False,
            "error": 0.0,
            "note": "帧精确模式：将重新编码，切割点完全准确，画质视觉无损。",
        }

    if not frames:
        return {
            "mode": "precise",
            "start": round(start, 3),
            "end": round(end, 3),
            "snapped": False,
            "error": 0.0,
            "note": "未取到关键帧列表，已自动退化为帧精确模式。",
        }

    s_kf, s_err = nearest_keyframe(start, frames)
    e_kf, e_err = nearest_keyframe(end, frames)
    if e_kf <= s_kf:
        e_kf = next((f for f in frames if f > s_kf), end)
        e_err = abs(e_kf - end)

    worst = max(s_err, e_err)
    if mode == "smart" and worst > SNAP_TOLERANCE:
        return {
            "mode": "precise",
            "start": round(start, 3),
            "end": round(end, 3),
            "snapped": False,
            "error": round(worst, 3),
            "note": (
                f"离最近关键帧最远差 {worst:.2f}s，超过 {SNAP_TOLERANCE}s 阈值，"
                "已自动改用帧精确模式以免出现黑帧。"
            ),
        }

    return {
        "mode": "lossless",
        "start": round(s_kf, 3),
        "end": round(e_kf, 3),
        "snapped": True,
        "error": round(worst, 3),
        "note": (
            f"已吸附到最近关键帧（最大误差 ±{worst:.2f}s），"
            "无损切割、秒级完成、零画质损失。"
        ),
    }


def trim_args(planned: dict) -> list[str]:
    """生成裁剪部分的 ffmpeg 参数（放在 -i 之前做快速定位）。"""
    start = float(planned.get("start") or 0.0)
    end = float(planned.get("end") or 0.0)
    dur = max(0.05, end - start)
    args = ["-ss", f"{start:.3f}"]
    if planned.get("mode") == "lossless":
        args += ["-t", f"{dur:.3f}", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        args += ["-t", f"{dur:.3f}"]
    return args


def encode_args_for_trim(planned: dict, *, use_nvenc: bool = False) -> list[str]:
    """帧精确裁剪时的视频编码参数。"""
    if planned.get("mode") == "lossless":
        return []
    return video_encode_args(crf=18, preset="veryfast", use_nvenc=use_nvenc)
