# -*- coding: utf-8 -*-
"""压缩：CRF 质量优先 / 目标体积（两遍 ABR）/ NVENC 硬件加速 / 分辨率限制。"""

from __future__ import annotations

import logging
import math

log = logging.getLogger("vedio.compress")

# x264 CRF -> NVENC cq 的经验映射（同数值下 NVENC 画质略弱，故 +2）
_NVENC_CQ_OFFSET = 2

# 每像素每帧比特数的粗略模型，用于体积预估（经验值）
_CRF_BPP = {
    16: 0.140,
    18: 0.105,
    20: 0.080,
    21: 0.066,
    22: 0.055,
    23: 0.046,
    24: 0.038,
    26: 0.028,
    28: 0.020,
}

PRESETS_X264 = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow")
PRESETS_NVENC = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")


def _bpp_for_crf(crf: int) -> float:
    if crf in _CRF_BPP:
        return _CRF_BPP[crf]
    keys = sorted(_CRF_BPP)
    if crf <= keys[0]:
        return _CRF_BPP[keys[0]]
    if crf >= keys[-1]:
        return _CRF_BPP[keys[-1]]
    lo = max(k for k in keys if k <= crf)
    hi = min(k for k in keys if k >= crf)
    if lo == hi:
        return _CRF_BPP[lo]
    ratio = (crf - lo) / (hi - lo)
    return _CRF_BPP[lo] + (_CRF_BPP[hi] - _CRF_BPP[lo]) * ratio


def video_encode_args(
    *,
    crf: int = 21,
    preset: str = "medium",
    use_nvenc: bool = False,
    target_kbps: int = 0,
    max_kbps: int = 0,
) -> list[str]:
    """视频编码参数。

    target_kbps>0 : ABR 目标码率（目标体积模式）
    max_kbps>0    : 码率上限。CRF 模式下配合使用即「受限 CRF」，
                    保证重编码后的体积不会超过原视频（源本身已压得很狠时尤其重要）。
    """
    cap = int(max_kbps or 0)

    if use_nvenc:
        p = preset if preset in PRESETS_NVENC else "p5"
        args = ["-c:v", "h264_nvenc", "-preset", p, "-pix_fmt", "yuv420p"]
        if target_kbps > 0:
            b = int(target_kbps)
            args += ["-rc", "vbr", "-b:v", f"{b}k",
                     "-maxrate", f"{int(b * 1.4)}k", "-bufsize", f"{int(b * 2.8)}k"]
        elif cap > 0:
            # NVENC 要让 maxrate 生效必须同时给目标码率
            args += ["-rc", "vbr", "-cq", str(int(crf) + _NVENC_CQ_OFFSET),
                     "-b:v", f"{cap}k", "-maxrate", f"{cap}k",
                     "-bufsize", f"{cap * 2}k"]
        else:
            args += ["-rc", "vbr", "-cq", str(int(crf) + _NVENC_CQ_OFFSET)]
        return args

    p = preset if preset in PRESETS_X264 else "medium"
    args = ["-c:v", "libx264", "-preset", p, "-pix_fmt", "yuv420p"]
    if target_kbps > 0:
        b = int(target_kbps)
        args += ["-b:v", f"{b}k", "-maxrate", f"{int(b * 1.4)}k",
                 "-bufsize", f"{int(b * 2.8)}k"]
    else:
        args += ["-crf", str(int(crf))]
        if cap > 0:
            args += ["-maxrate", f"{cap}k", "-bufsize", f"{cap * 2}k"]
    return args


def audio_encode_args(*, kbps: int = 128, mute: bool = False) -> list[str]:
    if mute:
        return ["-an"]
    return ["-c:a", "aac", "-b:a", f"{int(kbps)}k", "-ac", "2"]


def scale_filter(max_height: int, src_w: int, src_h: int) -> list[str]:
    """限制最大高度，保持宽高比；-2 保证宽为偶数（h264 要求）。"""
    if not max_height or not src_h or src_h <= max_height:
        return []
    return ["-vf", f"scale=-2:{int(max_height)}"]


def target_bitrates(target_mb: float, duration: float, audio_kbps: int = 128) -> tuple[int, int]:
    """由目标体积反推视频/音频码率（kbps）。"""
    if duration <= 0 or target_mb <= 0:
        return 0, audio_kbps
    total_kbps = (float(target_mb) * 8192.0) / duration
    if total_kbps <= audio_kbps + 40:
        audio_kbps = max(48, int(total_kbps * 0.25))
    video_kbps = max(80, int(total_kbps - audio_kbps))
    return video_kbps, int(audio_kbps)


def estimate(
    *,
    duration: float,
    src_size: int,
    src_w: int,
    src_h: int,
    src_video_bitrate: int,
    mode: str = "crf",
    crf: int = 21,
    max_height: int = 0,
    target_mb: float = 0,
    audio_kbps: int = 128,
    fps: float = 30.0,
    use_nvenc: bool = False,
) -> dict:
    """压缩后体积预估（仅供决策参考）。"""
    duration = float(duration or 0)
    if duration <= 0:
        return {"ok": False, "note": "无法取得时长，跳过预估"}

    out_h = int(max_height) if max_height and src_h and max_height < src_h else int(src_h or 0)
    out_w = int(round((src_w or 0) * out_h / src_h)) if (src_h and src_w and out_h) else int(src_w or 0)
    if out_w % 2:
        out_w += 1

    if mode == "target" and target_mb > 0:
        v_kbps, a_kbps = target_bitrates(target_mb, duration, audio_kbps)
        est = int((v_kbps + a_kbps) * 1000 / 8 * duration)
        note = f"目标体积模式：视频 {v_kbps} kbps + 音频 {a_kbps} kbps（两遍编码）"
    elif mode == "none":
        est = int(src_size or 0)
        v_kbps = int(src_video_bitrate / 1000) if src_video_bitrate else 0
        a_kbps = audio_kbps
        note = "不压缩：仅裁剪/转封装，体积按原码率按时长折算"
    else:
        fps_eff = fps or 30.0
        bpp = _bpp_for_crf(int(crf))
        model_bps = out_w * out_h * fps_eff * bpp
        src_bps = float(src_video_bitrate or 0)
        # 不高于源码率，避免预估离谱
        v_bps = model_bps if not src_bps else min(model_bps, src_bps * 1.05)
        v_kbps = int(v_bps / 1000)
        a_kbps = int(audio_kbps)
        est = int((v_kbps + a_kbps) * 1000 / 8 * duration)
        note = (
            f"CRF {crf} 质量优先模式：预估视频 {v_kbps} kbps"
            + (f"，已限制不超过源码率 {int(src_bps/1000)} kbps" if src_bps else "")
        )

    src_size = int(src_size or 0)
    ratio = (est / src_size) if src_size else 0.0
    if ratio >= 0.98 and mode == "crf":
        note += "；注意：源视频码率已经很低，此档位难以再压小，建议提高 CRF 或勾选限制分辨率"
    return {
        "ok": True,
        "duration": round(duration, 2),
        "src_size": src_size,
        "est_size": est,
        "ratio": round(ratio, 3),
        "video_kbps": v_kbps,
        "audio_kbps": a_kbps,
        "out_width": out_w,
        "out_height": out_h,
        "hw": "NVENC" if use_nvenc else "x264",
        "note": note + "；估算值仅供参考，实际以输出文件为准。",
    }
