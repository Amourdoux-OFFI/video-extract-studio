# -*- coding: utf-8 -*-
"""ffprobe 封装：本地文件探测、媒体信息摘要、关键帧列表。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.models import MediaInfo, StreamInfo, human_size
from app.media import runner

log = logging.getLogger("vedio.probe")


def probe_json(path: str | Path, timeout: float = 60.0) -> dict:
    ok, out = runner.run_probe(
        [
            "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        timeout=timeout,
    )
    if not ok or not out.strip():
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def _fps_of(stream: dict) -> float:
    for key in ("r_frame_rate", "avg_frame_rate"):
        v = stream.get(key) or ""
        if "/" in v:
            try:
                num, den = v.split("/", 1)
                den_f = float(den)
                if den_f:
                    return round(float(num) / den_f, 3)
            except Exception:  # noqa: BLE001
                continue
        try:
            return float(v)
        except Exception:  # noqa: BLE001
            continue
    return 0.0


def summarize(path: str | Path) -> dict:
    """给 UI 用的紧凑摘要。"""
    p = Path(path)
    data = probe_json(p)
    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for src in (fmt.get("duration"), (video or {}).get("duration")):
        try:
            duration = float(src)
            if duration > 0:
                break
        except Exception:  # noqa: BLE001
            continue

    size = 0
    try:
        size = int(fmt.get("size") or p.stat().st_size)
    except Exception:  # noqa: BLE001
        size = 0

    bitrate = 0
    try:
        bitrate = int(fmt.get("bit_rate") or 0)
    except Exception:  # noqa: BLE001
        bitrate = 0
    if not bitrate and duration > 0 and size:
        bitrate = int(size * 8 / duration)

    return {
        "path": str(p),
        "name": p.name,
        "exists": p.exists(),
        "size": size,
        "size_text": human_size(size),
        "duration": round(duration, 3),
        "bitrate": bitrate,
        "format": (fmt.get("format_name") or "").split(",")[0],
        "video": {
            "codec": (video or {}).get("codec_name", ""),
            "width": int((video or {}).get("width") or 0),
            "height": int((video or {}).get("height") or 0),
            "fps": _fps_of(video or {}),
            "bitrate": int((video or {}).get("bit_rate") or 0),
            "pix_fmt": (video or {}).get("pix_fmt", ""),
        }
        if video
        else None,
        "audio": {
            "codec": (audio or {}).get("codec_name", ""),
            "channels": int((audio or {}).get("channels") or 0),
            "sample_rate": int((audio or {}).get("sample_rate") or 0),
            "bitrate": int((audio or {}).get("bit_rate") or 0),
        }
        if audio
        else None,
    }


def keyframes(path: str | Path, max_count: int = 4000, timeout: float = 120.0) -> dict:
    """返回视频关键帧时间点（秒）。用于无损裁剪的刻度吸附。"""
    ok, out = runner.run_probe(
        [
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags",
            "-of", "csv=p=0",
            str(path),
        ],
        timeout=timeout,
    )
    times: list[float] = []
    if ok and out:
        for line in out.splitlines():
            line = line.strip()
            if not line or "," not in line:
                continue
            ts, _, flags = line.partition(",")
            if "K" not in flags.upper():
                continue
            try:
                times.append(round(float(ts), 3))
            except ValueError:
                continue
            if len(times) >= max_count:
                break
    times = sorted(set(t for t in times if t >= 0))

    if not times:
        # 退路：解码层跳过非关键帧
        ok2, out2 = runner.run_probe(
            [
                "-v", "error",
                "-select_streams", "v:0",
                "-skip_frame", "nokey",
                "-show_entries", "frame=pts_time",
                "-of", "csv=p=0",
                str(path),
            ],
            timeout=timeout,
        )
        if ok2 and out2:
            for line in out2.splitlines():
                try:
                    times.append(round(float(line.strip().rstrip(",")), 3))
                except ValueError:
                    continue
                if len(times) >= max_count:
                    break
        times = sorted(set(t for t in times if t >= 0))

    info = summarize(path)
    return {"duration": info.get("duration", 0.0), "keyframes": times}


def to_media_info(path: str | Path) -> MediaInfo:
    """把本地文件包装成统一的 MediaInfo，使其能复用同一套 UI 与处理链。"""
    p = Path(path)
    info = summarize(p)
    v = info.get("video") or {}
    a = info.get("audio") or {}
    stream = StreamInfo(
        stream_id="local",
        url=str(p),
        label=f"{v.get('height') or '?'}p" if v else "本地文件",
        width=int(v.get("width") or 0),
        height=int(v.get("height") or 0),
        fps=float(v.get("fps") or 0),
        vcodec=str(v.get("codec") or ""),
        acodec=str(a.get("codec") or ""),
        codec_family=str(v.get("codec") or ""),
        bandwidth=int(info.get("bitrate") or 0),
        filesize=int(info.get("size") or 0),
        is_audio_only=False,
        is_muxed=bool(a),
        extra={"info": info},
    )
    warnings = []
    if not p.exists():
        warnings.append("文件不存在")
    if not info.get("video"):
        warnings.append("未检测到视频流")

    return MediaInfo(
        id=f"local:{p}",
        platform="local",
        source_url=str(p),
        title=p.stem,
        author="本地文件",
        duration=float(info.get("duration") or 0),
        kind="video",
        source="ffprobe",
        local_path=str(p),
        streams=[stream],
        audios=[],
        warnings=warnings,
        raw={"summary": info},
    )
