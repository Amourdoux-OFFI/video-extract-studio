# -*- coding: utf-8 -*-
"""多连接分片下载器（httpx 异步 + Range + 断点续传 + 备用 URL 回退）。"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

ProgressCB = Callable[[int, int, float, float, float], None]  # done,total,pct,speed,eta


class DownloadError(RuntimeError):
    pass


class Canceled(RuntimeError):
    pass


class _Meter:
    """下载速率与 ETA 统计（线程安全）。"""

    def __init__(self, total: int, cb: ProgressCB | None, min_interval: float = 0.35):
        self.total = int(total or 0)
        self.done = 0
        self.cb = cb
        self.min_interval = min_interval
        self.speed = 0.0
        self._t_last = time.time()
        self._d_last = 0
        self._lock = threading.Lock()

    def add(self, n: int, force: bool = False) -> None:
        with self._lock:
            self.done += n
            now = time.time()
            dt = now - self._t_last
            if dt >= self.min_interval:
                self.speed = (self.done - self._d_last) / dt if dt > 0 else 0.0
                self._t_last = now
                self._d_last = self.done
                self._emit()
            elif force:
                self._emit()

    def _emit(self) -> None:
        if not self.cb:
            return
        total = self.total
        pct = (self.done / total * 100.0) if total else 0.0
        eta = ((total - self.done) / self.speed) if (self.speed > 0 and total) else 0.0
        try:
            self.cb(self.done, total, pct, self.speed, eta)
        except Exception:  # noqa: BLE001
            pass


def _merge_headers(stream_headers: dict | None, extra: dict | None = None) -> dict:
    h = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    for src in (stream_headers, extra):
        if src:
            for k, v in src.items():
                if v:
                    h[k] = v
    return h


async def _probe(client: httpx.AsyncClient, url: str, headers: dict) -> tuple[int, bool]:
    """返回 (总字节数, 是否支持 Range)。"""
    try:
        r = await client.get(url, headers={**headers, "Range": "bytes=0-0"})
        if r.status_code == 206:
            cr = r.headers.get("content-range", "")
            if "/" in cr:
                tail = cr.rsplit("/", 1)[-1].strip()
                if tail.isdigit():
                    return int(tail), True
        if r.status_code == 200:
            cl = r.headers.get("content-length")
            if cl and cl.isdigit():
                return int(cl), False
    except Exception:  # noqa: BLE001
        pass
    return 0, False


async def _download_range(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    start: int,
    end: int,
    part: Path,
    meter: _Meter,
    cancel: threading.Event,
    retries: int = 4,
) -> None:
    expect = end - start + 1
    if part.exists() and part.stat().st_size == expect:
        meter.add(expect)
        return

    last_exc: Exception | None = None
    for attempt in range(retries):
        if cancel.is_set():
            raise Canceled()
        offset = 0
        if part.exists():
            offset = part.stat().st_size
            if offset >= expect:
                meter.add(expect - min(offset, expect))
                return
        try:
            h = {**headers, "Range": f"bytes={start + offset}-{end}"}
            async with client.stream("GET", url, headers=h) as r:
                if r.status_code not in (200, 206):
                    raise DownloadError(f"HTTP {r.status_code}")
                mode = "ab" if offset else "wb"
                with part.open(mode) as f:
                    async for chunk in r.aiter_bytes(1 << 18):
                        if cancel.is_set():
                            raise Canceled()
                        f.write(chunk)
                        meter.add(len(chunk))
            if part.stat().st_size >= expect:
                return
            last_exc = DownloadError(
                f"分片不完整 {part.stat().st_size}/{expect}"
            )
        except Canceled:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            meter.add(0, force=True)
            await asyncio.sleep(min(2 ** attempt * 0.6, 6.0))

    raise DownloadError(f"分片下载失败 ({start}-{end}): {last_exc}")


async def _download_single(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    dest: Path,
    meter: _Meter,
    cancel: threading.Event,
    retries: int = 4,
) -> None:
    part = Path(str(dest) + ".part")
    last_exc: Exception | None = None
    for attempt in range(retries):
        if cancel.is_set():
            raise Canceled()
        offset = part.stat().st_size if part.exists() else 0
        try:
            h = dict(headers)
            if offset:
                h["Range"] = f"bytes={offset}-"
            async with client.stream("GET", url, headers=h) as r:
                if r.status_code not in (200, 206):
                    raise DownloadError(f"HTTP {r.status_code}")
                if r.status_code == 200 and offset:
                    offset = 0
                    meter.done = 0
                mode = "ab" if offset else "wb"
                with part.open(mode) as f:
                    async for chunk in r.aiter_bytes(1 << 18):
                        if cancel.is_set():
                            raise Canceled()
                        f.write(chunk)
                        meter.add(len(chunk))
            part.replace(dest)
            return
        except Canceled:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            await asyncio.sleep(min(2 ** attempt * 0.6, 6.0))
    raise DownloadError(str(last_exc))


async def download(
    url: str,
    dest: Path,
    *,
    headers: dict | None = None,
    backup_urls: list[str] | None = None,
    on_progress: ProgressCB | None = None,
    connections: int = 4,
    chunk_mb: int = 4,
    cancel: threading.Event | None = None,
    timeout: float = 60.0,
    proxy: str | None = None,
) -> int:
    """下载 url 到 dest，返回字节数。失败时自动尝试 backup_urls。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cancel = cancel or threading.Event()
    hdrs = _merge_headers(headers)
    chunk = max(1, int(chunk_mb)) * 1024 * 1024
    candidates = [url] + [u for u in (backup_urls or []) if u]

    last_exc: Exception | None = None
    for idx, target in enumerate(candidates):
        if cancel.is_set():
            raise Canceled()
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(timeout, connect=20.0),
                proxy=proxy or None,
                headers={"User-Agent": DEFAULT_UA},
            ) as client:
                total, ranged = await _probe(client, target, hdrs)
                meter = _Meter(total, on_progress)
                meter._emit()

                if ranged and total > chunk * 1.5:
                    n = max(1, min(int(connections), math.ceil(total / chunk)))
                    step = math.ceil(total / n)
                    parts: list[Path] = []
                    jobs = []
                    for i in range(n):
                        s = i * step
                        e = min(total - 1, s + step - 1)
                        if s > e:
                            break
                        p = Path(f"{dest}.part{i}")
                        parts.append(p)
                        jobs.append(
                            _download_range(client, target, hdrs, s, e, p, meter, cancel)
                        )
                    await asyncio.gather(*jobs)
                    with dest.open("wb") as out:
                        for p in parts:
                            with p.open("rb") as f:
                                while True:
                                    buf = f.read(1 << 20)
                                    if not buf:
                                        break
                                    out.write(buf)
                    for p in parts:
                        p.unlink(missing_ok=True)
                    got = dest.stat().st_size
                    if total and got != total:
                        raise DownloadError(f"合并后大小不符 {got}/{total}")
                else:
                    await _download_single(client, target, hdrs, dest, meter, cancel)
                    got = dest.stat().st_size if dest.exists() else 0

                meter.add(0, force=True)
                return dest.stat().st_size
        except Canceled:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if idx + 1 < len(candidates):
                await asyncio.sleep(0.5)

    raise DownloadError(f"下载失败：{last_exc}")


def sync_download(*args, **kwargs) -> int:
    """给非 async 上下文用的同步包装。"""
    return asyncio.run(download(*args, **kwargs))
