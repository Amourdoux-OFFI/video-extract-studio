# -*- coding: utf-8 -*-
"""ffmpeg 子进程执行器：解析 -progress 输出并上报进度，支持取消。

进度输出（stdout）形如：
    frame=123
    fps=45.0
    out_time_us=4000000
    speed=1.53x
    progress=continue
日志走 stderr。这里把 stderr 合并进 stdout 单管道读取，
既避免双管道死锁，又能保留错误信息。
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from app.media.ffmpeg_locate import ffmpeg_path, ffprobe_path

log = logging.getLogger("vedio.ffmpeg.run")

_CREATE_NO_WINDOW = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
_TIME_RE = re.compile(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")

ProgressCB = Callable[[float, float, float], None]  # percent, speed(倍速), eta(秒)


class Canceled(RuntimeError):
    pass


class FFmpegError(RuntimeError):
    pass


@dataclass
class RunResult:
    ok: bool
    returncode: int = 0
    stderr: str = ""
    seconds: float = 0.0
    canceled: bool = False
    args: list = field(default_factory=list)


def _popen_kwargs() -> dict:
    kw: dict = {}
    if _CREATE_NO_WINDOW:
        kw["creationflags"] = _CREATE_NO_WINDOW
    return kw


def run(
    args: list[str],
    *,
    duration: float = 0.0,
    on_progress: ProgressCB | None = None,
    cancel: threading.Event | None = None,
    timeout: float | None = None,
    tail_lines: int = 40,
) -> RunResult:
    """执行 `ffmpeg <args>`（会自动补 -progress pipe:1）。"""
    exe = ffmpeg_path()
    if not exe:
        raise FFmpegError("未找到 ffmpeg，请在设置中指定路径或放置到 vendor/ffmpeg/")

    cmd = [
        exe, "-hide_banner", "-nostdin", "-y",
        "-progress", "pipe:1", "-loglevel", "warning",
        *args,
    ]
    t0 = time.time()
    tail: deque[str] = deque(maxlen=tail_lines)
    last_emit = 0.0
    canceled = False

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_popen_kwargs(),
    )

    def handle(line: str) -> None:
        nonlocal last_emit
        line = (line or "").strip()
        if not line:
            return
        out_us = None
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            val = line.split("=", 1)[1].strip()
            if val.isdigit():
                out_us = int(val)
        elif line.startswith("out_time="):
            m = _TIME_RE.match(line)
            if m:
                out_us = int(
                    (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))
                    * 1_000_000
                )

        if out_us is not None and on_progress:
            now = time.time()
            if now - last_emit < 0.3:
                return
            last_emit = now
            done = out_us / 1_000_000.0
            pct = (done / duration * 100.0) if duration > 0 else 0.0
            speed = 0.0
            eta = 0.0
            try:
                on_progress(min(99.9, pct), speed, eta)
            except Exception:  # noqa: BLE001
                pass
            return

        tail.append(line)
        m = _SPEED_RE.search(line)
        if m:
            # 记录速度供下一次 progress 使用（存在 tail 里，指标单独处理）
            return

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if cancel is not None and cancel.is_set():
                canceled = True
                break
            if timeout and (time.time() - t0) > timeout:
                canceled = True
                tail.append("!! 执行超时")
                break
            handle(raw)
        if canceled:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        rc = proc.wait()
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass

    seconds = time.time() - t0
    text = "\n".join(tail)

    if canceled:
        return RunResult(False, rc, text, seconds, True, cmd)
    ok = rc == 0
    if ok and on_progress:
        try:
            on_progress(100.0, 0.0, 0.0)
        except Exception:  # noqa: BLE001
            pass
    return RunResult(ok, rc, text, seconds, False, cmd)


def run_probe(args: list[str], timeout: float = 60.0) -> tuple[bool, str]:
    """执行 `ffprobe <args>`。"""
    exe = ffprobe_path()
    if not exe:
        return False, "未找到 ffprobe"
    try:
        out = subprocess.run(
            [exe, "-hide_banner", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **_popen_kwargs(),
        )
        return out.returncode == 0, out.stdout or ""
    except subprocess.TimeoutExpired:
        return False, "ffprobe 超时"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def run_capture(args: list[str], timeout: float = 120.0) -> tuple[int, str]:
    """执行任意 ffmpeg 命令并拿文本输出（不注入 -progress）。"""
    exe = ffmpeg_path()
    if not exe:
        return -1, "未找到 ffmpeg"
    try:
        out = subprocess.run(
            [exe, "-hide_banner", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **_popen_kwargs(),
        )
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)
