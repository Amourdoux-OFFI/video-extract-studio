# -*- coding: utf-8 -*-
"""yt-dlp 兜底解析器。

说明：yt-dlp 的抖音 extractor 目前处于失效状态（上游 issue #17464），
因此它只作为 B站/其他站点的兜底，以及未来扩展新平台的入口。
"""

from __future__ import annotations

import asyncio
import logging

from app.core.models import MediaInfo, StreamInfo, codec_family_of
from app.extractors.base import DEFAULT_UA, ExtractError, Extractor

log = logging.getLogger("vedio.ytdlp")


def _ydl_opts() -> dict:
    from app import config

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "http_headers": {"User-Agent": DEFAULT_UA},
        "extractor_retries": 2,
    }
    if config.get("use_browser_cookie"):
        # ('browser', profile, key) —— 失败会被 yt-dlp 忽略
        opts["cookiesfrombrowser"] = ("chrome", None, None)
    return opts


def _extract_sync(url: str) -> dict:
    try:
        import yt_dlp
    except Exception as exc:  # noqa: BLE001
        raise ExtractError(f"yt-dlp 不可用：{exc}") from exc

    attempts = []
    base = _ydl_opts()
    if base.get("cookiesfrombrowser"):
        attempts.append(base)  # 先带浏览器 Cookie
    plain = dict(base)
    plain.pop("cookiesfrombrowser", None)
    attempts.append(plain)  # 失败则不带（Chrome 数据库常被占用）

    last: Exception | None = None
    for opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False) or {}
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise ExtractError(f"yt-dlp 解析失败：{last}")


class YtdlpFallbackExtractor(Extractor):
    name = "yt-dlp"
    platform = "other"
    priority = 90
    label = "yt-dlp 兜底"

    def match(self, url: str) -> bool:
        return bool(url) and url.lower().startswith("http")

    async def resolve(self, url: str) -> list[MediaInfo]:
        info = await asyncio.to_thread(_extract_sync, url)
        entries = info.get("entries")
        if entries:
            info = next((e for e in entries if e), info)

        streams: list[StreamInfo] = []
        audios: list[StreamInfo] = []
        warns = ["由 yt-dlp 兜底解析，清晰度可能不是最优"]

        for f in info.get("formats") or []:
            furl = f.get("url")
            if not furl:
                continue
            vcodec = str(f.get("vcodec") or "none")
            acodec = str(f.get("acodec") or "none")
            h = int(f.get("height") or 0)
            w = int(f.get("width") or 0)
            bw = int(f.get("tbr") or 0) * 1000 or int(f.get("bitrate") or 0)
            fam = codec_family_of(vcodec)
            fid = str(f.get("format_id") or len(streams))
            has_v = vcodec != "none"
            has_a = acodec != "none"

            if has_v and has_a:
                streams.append(
                    StreamInfo(
                        stream_id=f"yt-{fid}",
                        url=furl,
                        label=f.get("format_note") or (f"{h}P" if h else fid),
                        width=w,
                        height=h,
                        fps=float(f.get("fps") or 0),
                        vcodec=vcodec,
                        acodec=acodec,
                        codec_family=fam,
                        bandwidth=bw,
                        filesize=int(f.get("filesize") or f.get("filesize_approx") or 0),
                        is_muxed=True,
                        headers={"User-Agent": DEFAULT_UA},
                    )
                )
            elif has_v and not has_a:
                streams.append(
                    StreamInfo(
                        stream_id=f"yt-{fid}",
                        url=furl,
                        label=(f.get("format_note") or "") + (" 仅视频" if h else "仅视频"),
                        width=w,
                        height=h,
                        fps=float(f.get("fps") or 0),
                        vcodec=vcodec,
                        codec_family=fam,
                        bandwidth=bw,
                        is_muxed=False,
                        headers={"User-Agent": DEFAULT_UA},
                    )
                )
            elif has_a and not has_v:
                audios.append(
                    StreamInfo(
                        stream_id=f"yta-{fid}",
                        url=furl,
                        label=f.get("format_note") or fid,
                        acodec=acodec,
                        codec_family=codec_family_of(acodec),
                        bandwidth=bw,
                        is_audio_only=True,
                        is_muxed=False,
                        headers={"User-Agent": DEFAULT_UA},
                    )
                )

        streams.sort(key=lambda s: (s.height or 0, s.bandwidth or 0), reverse=True)
        audios.sort(key=lambda s: s.bandwidth or 0, reverse=True)
        if not streams:
            raise ExtractError("yt-dlp 未解析出任何视频格式")

        return [
            MediaInfo(
                id=f"ytdlp:{info.get('id') or abs(hash(url)) % 10**10}",
                platform=self.platform,
                source_url=url,
                title=str(info.get("title") or "").strip(),
                author=str(info.get("uploader") or info.get("channel") or "").strip(),
                cover=str(info.get("thumbnail") or ""),
                duration=float(info.get("duration") or 0),
                kind="video",
                source=self.name,
                streams=streams,
                audios=audios,
                warnings=warns,
                raw={"extractor": info.get("extractor")},
            )
        ]
