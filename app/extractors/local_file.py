# -*- coding: utf-8 -*-
"""本地文件解析器：把电脑上的视频文件包装成统一 MediaInfo。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.core.models import MediaInfo
from app.extractors.base import ExtractError, Extractor
from app.media import probe as P

log = logging.getLogger("vedio.local")

VIDEO_EXT = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v",
    ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".rmvb", ".vob", ".ogv",
}


def is_video_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXT


class LocalFileExtractor(Extractor):
    name = "local-file"
    platform = "local"
    priority = 5
    label = "本地文件"

    def match(self, url: str) -> bool:
        if not url:
            return False
        if url.lower().startswith(("http://", "https://")):
            return False
        return Path(url).exists()

    async def resolve(self, url: str) -> list[MediaInfo]:
        p = Path(url)
        if not p.exists():
            raise ExtractError(f"文件不存在：{url}")
        if not p.is_file():
            raise ExtractError(f"不是文件：{url}")
        return [await asyncio.to_thread(P.to_media_info, p)]


def scan_paths(paths: list[str]) -> tuple[list[MediaInfo], list[dict]]:
    """批量导入本地文件/文件夹。"""
    items: list[MediaInfo] = []
    errors: list[dict] = []
    seen: set[str] = set()

    def handle(fp: Path) -> None:
        key = str(fp.resolve()).lower()
        if key in seen:
            return
        seen.add(key)
        if not is_video_file(fp):
            return
        try:
            items.append(P.to_media_info(fp))
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": str(fp), "error": str(exc)})

    for raw in paths or []:
        p = Path(str(raw).strip().strip('"'))
        try:
            if p.is_dir():
                for fp in sorted(p.rglob("*")):
                    if fp.is_file():
                        handle(fp)
            elif p.is_file():
                handle(p)
            else:
                errors.append({"path": str(p), "error": "路径不存在"})
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": str(p), "error": str(exc)})
    return items, errors
