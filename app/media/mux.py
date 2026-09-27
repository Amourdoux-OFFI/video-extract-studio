# -*- coding: utf-8 -*-
"""DASH 合流：把分离的视频轨与音频轨无损封装成 mp4。"""

from __future__ import annotations

import logging
from pathlib import Path

from app.media import runner

log = logging.getLogger("vedio.mux")


def mux(
    video_path: str | Path,
    audio_path: str | Path | None,
    out_path: str | Path,
    *,
    duration: float = 0.0,
    on_progress=None,
    cancel=None,
) -> runner.RunResult:
    """`-c copy` 无损合流（不重新编码，秒级完成）。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    args: list[str] = ["-fflags", "+genpts", "-i", str(video_path)]
    if audio_path:
        args += ["-i", str(audio_path)]
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        args += ["-map", "0:v:0"]

    args += [
        "-c", "copy",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
    ]

    if Path(video_path).suffix.lower() not in (".mp4", ".m4v"):
        # m4s 等无扩展名/异扩展名输入统一输出 mp4
        args += ["-f", "mp4"]

    args.append(str(out))

    result = runner.run(
        args, duration=duration, on_progress=on_progress, cancel=cancel
    )
    if not result.ok:
        raise runner.FFmpegError(f"合流失败：{result.stderr[-500:]}")
    if not out.exists() or out.stat().st_size == 0:
        raise runner.FFmpegError("合流后文件为空")
    return result
