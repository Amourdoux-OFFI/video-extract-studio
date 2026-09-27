# -*- coding: utf-8 -*-
"""Extractor 抽象：所有平台解析器实现同一接口，便于降级链编排。"""

from __future__ import annotations

from app.core.models import MediaInfo

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class ExtractError(RuntimeError):
    """解析失败，携带面向用户的中文原因。"""


class Extractor:
    name: str = "base"
    platform: str = "other"
    priority: int = 100  # 数字越小越先尝试
    label: str = ""

    def match(self, url: str) -> bool:
        raise NotImplementedError

    async def resolve(self, url: str) -> list[MediaInfo]:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.name}>"
