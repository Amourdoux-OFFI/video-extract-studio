# -*- coding: utf-8 -*-
"""统一媒体数据模型：所有 Extractor 都必须产出 MediaInfo。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

PLATFORM_NAMES = {
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",
    "local": "本地文件",
    "other": "其他",
}


def human_size(n: int | float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def safe_filename(name: str, max_len: int = 120) -> str:
    """去掉 Windows 非法字符，压缩空白，限制长度。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "未命名"


@dataclass
class StreamInfo:
    """一条可下载的媒体流。"""

    stream_id: str
    url: str = ""
    label: str = ""
    quality_id: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    codec_family: str = ""
    bandwidth: int = 0
    filesize: int = 0
    is_audio_only: bool = False
    is_muxed: bool = True
    headers: dict = field(default_factory=dict)
    backup_urls: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def resolution(self) -> str:
        if self.height:
            return f"{self.width}x{self.height}" if self.width else f"{self.height}p"
        return ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["resolution"] = self.resolution
        d.pop("headers", None)  # 不回传给前端
        d.pop("extra", None)
        d.pop("url", None)
        d.pop("backup_urls", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "StreamInfo":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class MediaInfo:
    """一个作品/一个文件。"""

    id: str
    platform: str = "other"
    source_url: str = ""
    title: str = ""
    author: str = ""
    cover: str = ""
    duration: float = 0.0
    kind: str = "video"  # video | images
    source: str = ""
    local_path: str = ""
    streams: list = field(default_factory=list)
    audios: list = field(default_factory=list)
    images: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    # ---------- 便捷访问 ----------

    @property
    def platform_name(self) -> str:
        return PLATFORM_NAMES.get(self.platform, self.platform or "其他")

    def get_stream(self, stream_id: str) -> StreamInfo | None:
        for s in list(self.streams) + list(self.audios):
            if s.stream_id == stream_id:
                return s
        return None

    def best_stream(self, prefer_codec: str = "h264") -> StreamInfo | None:
        if not self.streams:
            return None

        def key(s: StreamInfo):
            fam_rank = {"h264": 0, "hevc": 1, "av1": 2}.get(s.codec_family, 3)
            prefer_rank = 0 if s.codec_family == prefer_codec else 1
            # 先比实际像素高度，再比码率；同档优先偏好编码
            return (s.height or 0, s.bandwidth or 0, -prefer_rank, -fam_rank)

        return max(self.streams, key=key)

    def best_audio(self) -> StreamInfo | None:
        if not self.audios:
            return None
        return max(self.audios, key=lambda s: s.bandwidth or 0)

    # ---------- 序列化 ----------

    def to_dict(self, include_secret: bool = False) -> dict:
        d = {
            "id": self.id,
            "platform": self.platform,
            "platform_name": self.platform_name,
            "source_url": self.source_url,
            "title": self.title,
            "author": self.author,
            "cover": self.cover,
            "duration": self.duration,
            "kind": self.kind,
            "source": self.source,
            "local_path": self.local_path,
            "warnings": list(self.warnings),
            "images": list(self.images),
            "streams": [
                s.to_dict() if not include_secret else asdict(s) for s in self.streams
            ],
            "audios": [
                s.to_dict() if not include_secret else asdict(s) for s in self.audios
            ],
        }
        return d

    def to_store(self) -> dict:
        """带 URL/headers 的完整快照，用于进程内缓存。"""
        d = self.to_dict(include_secret=True)
        d["_type"] = "MediaInfo"
        d["raw"] = {}
        return d

    @classmethod
    def from_store(cls, d: dict) -> "MediaInfo":
        d = dict(d)
        d.pop("_type", None)
        d.pop("platform_name", None)
        d.pop("resolution", None)
        d["streams"] = [StreamInfo.from_dict(x) for x in d.get("streams", [])]
        d["audios"] = [StreamInfo.from_dict(x) for x in d.get("audios", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def guess_platform(url: str) -> str:
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:  # noqa: BLE001
        return "other"
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return "douyin"
    if host.endswith("bilibili.com") or host == "b23.tv":
        return "bilibili"
    return "other"


QUALITY_LABELS_BILI = {
    127: "8K 超高清",
    126: "杜比视界",
    125: "HDR 真彩",
    120: "4K 超清",
    116: "1080P60",
    112: "1080P+",
    80: "1080P 高清",
    74: "720P60",
    64: "720P 高清",
    32: "480P 清晰",
    16: "360P 流畅",
}


def quality_label_bili(qid: int, height: int = 0, fps: float = 0.0) -> str:
    base = QUALITY_LABELS_BILI.get(qid)
    if base:
        return base
    if height:
        tag = f"{height}P"
        if fps and fps >= 50:
            tag += f"{int(round(fps))}"
        return tag
    return f"Q{qid}"


CODEC_FAMILY_BILI = {7: "h264", 12: "hevc", 13: "av1"}


def codec_family_of(codecs: str, codecid: int = 0) -> str:
    c = (codecs or "").lower()
    if c.startswith("avc") or c.startswith("h264"):
        return "h264"
    if c.startswith("hev") or c.startswith("hvc") or c.startswith("h265"):
        return "hevc"
    if c.startswith("av01") or c.startswith("av1"):
        return "av1"
    if c.startswith("vp9") or c.startswith("vp09"):
        return "vp9"
    if c.startswith("mp4a") or c.startswith("aac"):
        return "aac"
    if c.startswith("opus"):
        return "opus"
    return CODEC_FAMILY_BILI.get(codecid, "")
