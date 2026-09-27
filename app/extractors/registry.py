# -*- coding: utf-8 -*-
"""解析器注册表 + 降级链编排 + 解析结果缓存（解析与下载之间的交接）。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from app.core.link_extract import classify, extract_urls
from app.core.models import MediaInfo
from app.extractors.base import ExtractError, Extractor
from app.extractors.bilibili import BilibiliExtractor
from app.extractors.douyin import DouyinApiExtractor, DouyinExtractor
from app.extractors.local_file import LocalFileExtractor
from app.extractors.ytdlp_fallback import YtdlpFallbackExtractor

log = logging.getLogger("vedio.registry")

# -------------------- 降级链 --------------------
# priority 越小越先尝试；同一平台按顺序依次回退
EXTRACTORS: list[Extractor] = [
    LocalFileExtractor(),      # 5  本地文件
    DouyinExtractor(),         # 10 抖音主路（f2 签名）
    BilibiliExtractor(),       # 10 B站主路（官方接口）
    DouyinApiExtractor(),      # 60 抖音兜底：自定义解析接口（需配置）
    YtdlpFallbackExtractor(),  # 90 通用兜底
]

STORE_TTL = 1800.0
_store: dict[str, tuple[float, MediaInfo]] = {}
_store_lock = threading.RLock()


def put(media: MediaInfo) -> None:
    with _store_lock:
        _store[media.id] = (time.time(), media)


def get(media_id: str) -> MediaInfo | None:
    with _store_lock:
        row = _store.get(media_id)
        if not row:
            return None
        ts, media = row
        if time.time() - ts > STORE_TTL:
            _store.pop(media_id, None)
            return None
        return media


def store_size() -> int:
    with _store_lock:
        return len(_store)


def prune() -> int:
    now = time.time()
    with _store_lock:
        dead = [k for k, (ts, _) in _store.items() if now - ts > STORE_TTL]
        for k in dead:
            _store.pop(k, None)
    return len(dead)


# -------------------- 单链接解析 --------------------


def candidates(url: str) -> list[Extractor]:
    matched = [e for e in EXTRACTORS if _safe_match(e, url)]
    matched.sort(key=lambda e: e.priority)
    return matched


def _safe_match(e: Extractor, url: str) -> bool:
    try:
        return bool(e.match(url))
    except Exception:  # noqa: BLE001
        return False


async def resolve_url(url: str) -> tuple[list[MediaInfo], list[dict]]:
    """按降级链依次尝试，成功即返回。"""
    tried: list[dict] = []
    chain = candidates(url)
    if not chain:
        return [], [{"url": url, "extractor": "-", "error": "没有匹配的解析器"}]

    for ex in chain:
        try:
            items = await ex.resolve(url)
            if items:
                for it in items:
                    put(it)
                if tried:
                    for it in items:
                        it.warnings.append(
                            "主解析器失败，已自动降级到 " + (ex.label or ex.name)
                        )
                return items, tried
            tried.append({"url": url, "extractor": ex.name, "error": "返回空结果"})
        except ExtractError as exc:
            log.warning("[%s] 解析失败 %s：%s", ex.name, url, exc)
            tried.append({"url": url, "extractor": ex.name, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] 解析异常", ex.name)
            tried.append(
                {"url": url, "extractor": ex.name, "error": f"{type(exc).__name__}: {exc}"}
            )
    return [], tried


# -------------------- 批量解析（粘贴文本） --------------------


async def resolve_text(text: str, concurrency: int = 3) -> dict:
    urls = extract_urls(text or "")
    if not urls:
        return {"items": [], "unrecognized": [], "errors": [], "found": 0}

    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[int, dict] = {}

    async def one(idx: int, url: str) -> None:
        async with sem:
            plat = classify(url)
            if plat == "unknown":
                results[idx] = {"unrecognized": url}
                return
            try:
                items, errs = await resolve_url(url)
            except Exception as exc:  # noqa: BLE001
                results[idx] = {"error": {"url": url, "error": str(exc)}}
                return
            if items:
                results[idx] = {"items": items}
            else:
                msg = "; ".join(
                    f"{e.get('extractor')}: {e.get('error')}" for e in (errs or [])
                ) or "未知原因"
                results[idx] = {"error": {"url": url, "error": msg}}

    await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)))

    items: list[MediaInfo] = []
    errors: list[dict] = []
    unrecognized: list[str] = []
    for i in sorted(results):
        row = results[i]
        if "items" in row:
            items.extend(row["items"])
        elif "error" in row:
            errors.append(row["error"])
        else:
            unrecognized.append(row["unrecognized"])

    return {
        "items": items,
        "unrecognized": unrecognized,
        "errors": errors,
        "found": len(urls),
    }


# -------------------- 供 /api/download 使用 --------------------


def pick(media_id: str, stream_id: str, audio_id: str | None = None):
    """取出 (media, video_stream, audio_stream)。"""
    media = get(media_id)
    if media is None:
        raise ExtractError("解析结果已过期，请重新解析该链接")
    stream = media.get_stream(stream_id)
    if stream is None:
        stream = media.best_stream()
    if stream is None:
        raise ExtractError("该作品没有可下载的视频流")
    audio = None
    if audio_id:
        audio = media.get_stream(audio_id)
    if audio is None and not stream.is_muxed:
        audio = media.best_audio()
    return media, stream, audio
