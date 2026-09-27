# -*- coding: utf-8 -*-
"""定位 ffmpeg / ffprobe，并探测可用编码器。

查找顺序：
  1. config.json 的 ffmpeg_path（可指向目录或可执行文件）
  2. <ROOT>/vendor/ffmpeg
  3. <BUNDLE>/vendor/ffmpeg   （PyInstaller 打包后的内置资源）
  4. 系统 PATH
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from app import config

log = logging.getLogger("vedio.ffmpeg")

_lock = threading.RLock()
_cache: dict = {"ffmpeg": None, "ffprobe": None, "searched": False}
_nvenc_cache: dict = {}


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _from_dir(d: Path, name: str) -> str | None:
    if not d:
        return None
    try:
        if not d.exists():
            return None
    except OSError:
        return None
    if d.is_file():
        return str(d) if d.name.lower().startswith(name) else None
    for cand in (d / _exe(name), d / name):
        if cand.is_file():
            return str(cand)
    return None


def _search() -> tuple[str | None, str | None]:
    override = str(config.get("ffmpeg_path") or "").strip()
    dirs: list[Path] = []
    if override:
        dirs.append(Path(override))
    dirs.append(config.ROOT / "vendor" / "ffmpeg")
    if config.BUNDLE != config.ROOT:
        dirs.append(config.BUNDLE / "vendor" / "ffmpeg")

    ff = fp = None
    for d in dirs:
        ff = ff or _from_dir(d, "ffmpeg")
        fp = fp or _from_dir(d, "ffprobe")
        if ff and fp:
            break

    if not ff:
        ff = shutil.which("ffmpeg")
    if not fp:
        fp = shutil.which("ffprobe")
    return ff, fp


def _ensure() -> None:
    with _lock:
        if not _cache["searched"]:
            ff, fp = _search()
            _cache.update(ffmpeg=ff, ffprobe=fp, searched=True)
            if ff:
                log.info("ffmpeg = %s", ff)
            else:
                log.warning("未找到 ffmpeg")
            if fp:
                log.info("ffprobe = %s", fp)


def reset() -> None:
    with _lock:
        _cache.update(ffmpeg=None, ffprobe=None, searched=False)
        _nvenc_cache.clear()


def ffmpeg_path() -> str | None:
    _ensure()
    return _cache["ffmpeg"]


def ffprobe_path() -> str | None:
    _ensure()
    return _cache["ffprobe"]


def _run_quiet(args: list[str], timeout: int = 20) -> str:
    try:
        out = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception:  # noqa: BLE001
        return ""


def has_nvenc() -> bool:
    """是否可用 h264_nvenc（编码器存在 + 能真正初始化）。"""
    ff = ffmpeg_path()
    if not ff:
        return False
    with _lock:
        if ff in _nvenc_cache:
            return _nvenc_cache[ff]
    ok = False
    text = _run_quiet([ff, "-hide_banner", "-encoders"])
    if "h264_nvenc" in text:
        # 真跑一次空转编码，确认驱动可用
        trial = _run_quiet(
            [
                ff, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.2",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            timeout=40,
        )
        ok = "error" not in trial.lower() or trial.strip() == ""
    with _lock:
        _nvenc_cache[ff] = ok
    log.info("NVENC 可用 = %s", ok)
    return ok


def probe_tools() -> dict:
    ff = ffmpeg_path()
    fp = ffprobe_path()
    version = ""
    if ff:
        first = _run_quiet([ff, "-hide_banner", "-version"]).splitlines()
        version = first[0].strip() if first else ""
    return {
        "has_ffmpeg": bool(ff),
        "has_ffprobe": bool(fp),
        "ffmpeg_path": ff or "",
        "ffprobe_path": fp or "",
        "ffmpeg_version": version,
        "has_nvenc": has_nvenc() if ff else False,
    }
