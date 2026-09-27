# -*- coding: utf-8 -*-
"""抖音解析器（主路：f2 + 自研无水印适配）。

链路：
    v.douyin.com/xxx --302--> iesdouyin.com/share/video/{aweme_id}
    -> f2 DouyinCrawler（a_bogus 签名 + 游客 Token）请求 web 详情接口
    -> 取 video.bit_rate[] 中 format=mp4 且码率最高的一档  <-- 关键：无水印
    -> 下载时必须带 Referer: https://www.douyin.com/

实测结论（务必保留，勿改回 download_addr）：
  * bit_rate[] 最高档        : 1920x1080 / 无水印 / 自带音轨
  * video.download_addr      : 1280x720 / 带抖音logo+抖音号+作者名
  * 无 Referer 请求 CDN 直链 : 403 Forbidden
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from app.core.link_extract import direct_aweme_id, is_short_douyin
from app.core.models import MediaInfo, StreamInfo
from app.cookies import douyin_cookie
from app.extractors.base import DEFAULT_UA, ExtractError, Extractor

log = logging.getLogger("vedio.douyin")

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_AWEME_FALLBACK = re.compile(r"/(?:video|note|slides)/(\d{6,})")
_RES_IN_GEAR = re.compile(r"(\d{3,4})")


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _silence_f2() -> None:
    """接管 f2 的日志，避免它污染当前工作目录。

    成因：f2/log/logger.py 在 import 时执行 `Path("./logs").mkdir()`，
    路径相对**当前工作目录**。因此只要从别的目录启动本程序，f2 就会
    在那里凭空建一个 logs/ 目录（实测会在工作区根目录留下一堆空日志）。

    做法：在 import f2 之前先给 "f2" logger 挂上自己的 handler。
    f2 的 log_setup() 开头有 `if logger.hasHandlers(): return`，
    于是它不会再创建 ./logs。同时把它的日志收进项目内的 logs/f2/。
    """
    import logging
    import logging.handlers

    from app import config

    lg = logging.getLogger("f2")
    if not lg.handlers:
        try:
            d = config.LOG_DIR / "f2"
            d.mkdir(parents=True, exist_ok=True)
            h = logging.handlers.RotatingFileHandler(
                d / "f2.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
            )
            h.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
            )
            lg.addHandler(h)
        except Exception:  # noqa: BLE001
            lg.addHandler(logging.NullHandler())
    lg.setLevel(logging.CRITICAL)
    lg.propagate = False


_guest_lock = __import__("threading").RLock()
_guest_cache: tuple[float, str] | None = None
_GUEST_TTL = 600.0  # 游客 Token 复用 10 分钟，省掉每次解析约 20s 的生成开销


def guest_cookie(force: bool = False) -> str:
    """用 f2 的 TokenManager 现造一份游客 Cookie（无需登录），带缓存。"""
    global _guest_cache
    import time

    with _guest_lock:
        if not force and _guest_cache and time.time() - _guest_cache[0] < _GUEST_TTL:
            return _guest_cache[1]
    ck = _build_guest_cookie()
    with _guest_lock:
        if ck:
            _guest_cache = (time.time(), ck)
    return ck


def _build_guest_cookie() -> str:
    _silence_f2()
    try:
        from f2.apps.douyin.utils import TokenManager, VerifyFpManager

        parts = []
        try:
            parts.append(f"msToken={TokenManager.gen_real_msToken()}")
        except Exception:  # noqa: BLE001
            parts.append(f"msToken={TokenManager.gen_false_msToken()}")
        for name, fn in (
            ("ttwid", TokenManager.gen_ttwid),
            ("webid", TokenManager.gen_webid),
            ("s_v_web_id", VerifyFpManager.gen_s_v_web_id),
        ):
            try:
                parts.append(f"{name}={fn()}")
            except Exception:  # noqa: BLE001
                pass
        return "; ".join(parts)
    except Exception as exc:  # noqa: BLE001
        log.warning("生成游客 Cookie 失败：%s", exc)
        return ""


def _cookie() -> str:
    return douyin_cookie() or guest_cookie()


def _headers(cookie: str) -> dict:
    h = {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


# --------------------------------------------------------------------------
# f2 调用（放独立线程 + 独立事件循环，隔离干扰）
# --------------------------------------------------------------------------


async def _f2_detail_async(aweme_id: str, cookie: str) -> dict:
    _silence_f2()
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import PostDetail
    kwargs = {
        "cookie": cookie,
        "headers": {
            "User-Agent": DEFAULT_UA,
            "Referer": "https://www.douyin.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        "proxies": {"http://": None, "https://": None},
        "max_tasks": 4,
        "max_connections": 4,
        "max_retries": 3,
        "timeout": 20,
    }
    async with DouyinCrawler(kwargs) as crawler:
        return await crawler.fetch_post_detail(PostDetail(aweme_id=aweme_id))


def _f2_detail_sync(aweme_id: str, cookie: str) -> dict:
    return asyncio.run(_f2_detail_async(aweme_id, cookie))


# 抖音接口需要限速：并发请求容易触发风控
_dy_gate: asyncio.Semaphore | None = None
_dy_last_at = 0.0
_MIN_INTERVAL = 1.2


def _gate() -> asyncio.Semaphore:
    global _dy_gate
    if _dy_gate is None:
        _dy_gate = asyncio.Semaphore(1)
    return _dy_gate


async def _throttle() -> None:
    """串行化 + 最小间隔，降低被风控概率。"""
    global _dy_last_at
    async with _gate():
        now = asyncio.get_running_loop().time()
        wait = _MIN_INTERVAL - (now - _dy_last_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _dy_last_at = asyncio.get_running_loop().time()


def _is_risk_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(k in text for k in ("403", "risk", "forbidden", "风控", "1028", "verify"))


async def fetch_detail(aweme_id: str, cookie: str | None = None) -> dict:
    """取作品详情。失败自动换新游客 Token 重试（抖音风控是常态，不是异常）。"""
    manual = cookie if cookie is not None else douyin_cookie()
    attempts = 4
    last: Exception | None = None
    empty_status: object = None

    for i in range(attempts):
        await _throttle()
        ck = manual or (guest_cookie(force=(i > 0)) if i else guest_cookie())
        try:
            raw = await asyncio.to_thread(_f2_detail_sync, aweme_id, ck)
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("抖音详情第 %d 次失败：%s", i + 1, exc)
            if not _is_risk_error(exc) and i >= 1:
                break
            await asyncio.sleep(0.8 * (i + 1))
            continue

        if isinstance(raw, dict):
            detail = raw.get("aweme_detail")
            if detail:
                return detail
            empty_status = raw.get("status_code")

        await asyncio.sleep(0.8 * (i + 1))

    if last is not None:
        hint = "（接口风控/限流）" if _is_risk_error(last) else ""
        raise ExtractError(f"抖音详情接口请求失败{hint}：{last}") from last
    raise ExtractError(
        f"抖音未返回作品数据（status_code={empty_status}），"
        "通常是接口风控或作品已删除；可在「设置」中填入抖音 Cookie 后重试"
    )


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------


class DouyinExtractor(Extractor):
    name = "douyin-f2"
    platform = "douyin"
    priority = 10
    label = "抖音官方接口（f2 签名）"

    def match(self, url: str) -> bool:
        return "douyin.com" in url or "iesdouyin.com" in url

    async def resolve(self, url: str) -> list[MediaInfo]:
        aweme_id = await self.aweme_id(url)
        detail = await fetch_detail(aweme_id)
        return [self._build(detail, url, aweme_id)]

    # ---------- 短链 -> aweme_id ----------

    async def aweme_id(self, url: str) -> str:
        direct = direct_aweme_id(url)
        if direct:
            return direct
        if not is_short_douyin(url) and "douyin.com" not in url:
            raise ExtractError("不是有效的抖音链接")
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=httpx.Timeout(20.0, connect=12.0)
            ) as client:
                r = await client.get(
                    url,
                    headers={
                        "User-Agent": MOBILE_UA,
                        "Accept": "text/html,application/xhtml+xml,*/*",
                    },
                )
                final = str(r.url)
        except Exception as exc:  # noqa: BLE001
            raise ExtractError(f"抖音短链跳转失败：{exc}") from exc

        got = direct_aweme_id(final)
        if got:
            return got
        m = _AWEME_FALLBACK.search(final)
        if m:
            return m.group(1)
        m = re.search(r"(\d{15,})", final)
        if m:
            return m.group(1)
        raise ExtractError(f"无法从短链解析出作品 ID：{final[:120]}")

    # ---------- 组装 ----------

    def _build(self, detail: dict, src_url: str, aweme_id: str) -> MediaInfo:
        video = detail.get("video") or {}
        author = str((detail.get("author") or {}).get("nickname") or "").strip()
        desc = str(detail.get("desc") or "").strip()
        try:
            duration = float(detail.get("duration") or 0) / 1000.0
        except (TypeError, ValueError):
            duration = 0.0

        cover = ""
        for key in ("cover", "origin_cover", "dynamic_cover"):
            node = video.get(key) or {}
            urls = node.get("url_list") or []
            if urls:
                cover = str(urls[0])
                break

        cookie = _cookie()
        hdrs = _headers(cookie)
        warns: list[str] = []

        # ---- 图集作品 ----
        images = detail.get("images") or []
        if images:
            urls = []
            for img in images:
                node = img if isinstance(img, dict) else {}
                lst = node.get("url_list") or []
                if lst:
                    urls.append(str(lst[-1]))
            warns.append("该链接是图集作品，暂只提供图片列表，不产出视频")
            return MediaInfo(
                id=f"douyin:{aweme_id}",
                platform="douyin",
                source_url=src_url,
                title=desc or f"图集 {aweme_id}",
                author=author,
                cover=cover,
                duration=0.0,
                kind="images",
                source=self.name,
                images=urls,
                warnings=warns,
                raw={"aweme_id": aweme_id, "images": len(urls)},
            )

        streams = self._streams(video, hdrs)
        if not streams:
            raise ExtractError("该作品没有可用的无水印视频流（可能是直播回放或受限内容）")

        best_h = max((s.height or 0) for s in streams)
        if best_h and best_h < 720:
            warns.append(f"该作品最高仅有 {best_h}P，源本身未提供更高清晰度")
        if video.get("is_h265"):
            warns.append("源视频为 H.265 编码，部分播放器可能不兼容")

        return MediaInfo(
            id=f"douyin:{aweme_id}",
            platform="douyin",
            source_url=src_url,
            title=desc or f"抖音作品 {aweme_id}",
            author=author,
            cover=cover,
            duration=duration,
            kind="video",
            source=self.name,
            streams=streams,
            audios=[],  # mp4 档位自带音轨，无需独立音轨
            warnings=warns,
            raw={"aweme_id": aweme_id, "login": bool(douyin_cookie())},
        )

    def _streams(self, video: dict, hdrs: dict) -> list[StreamInfo]:
        """从 bit_rate[] 里挑出无水印、自带音轨的 mp4 档位，按分辨率去重。"""
        best: dict[int, dict] = {}

        for item in video.get("bit_rate") or []:
            if str(item.get("format") or "").lower() != "mp4":
                continue  # dash 档位是纯视频轨，跳过
            urls = ((item.get("play_addr") or {}).get("url_list")) or []
            urls = [u for u in urls if u]
            if not urls:
                continue
            gear = str(item.get("gear_name") or "")
            m = _RES_IN_GEAR.search(gear)
            res = int(m.group(1)) if m else 0
            bw = int(item.get("bit_rate") or 0)
            key = res or bw
            if key not in best or bw > int(best[key].get("bit_rate") or 0):
                best[key] = {**item, "_urls": urls, "_res": res}

        out: list[StreamInfo] = []
        for item in best.values():
            res = int(item.get("_res") or 0)
            bw = int(item.get("bit_rate") or 0)
            urls = item["_urls"]
            out.append(
                StreamInfo(
                    stream_id=f"dy-{res or 'x'}-{bw}",
                    url=urls[0],
                    label=f"{res}P" if res else f"{int(bw/1000)}kbps",
                    quality_id=res,
                    width=0,
                    height=res,
                    fps=float(item.get("FPS") or 0),
                    vcodec="h264",
                    codec_family="h264",
                    bandwidth=bw,
                    filesize=0,
                    is_audio_only=False,
                    is_muxed=True,
                    headers=hdrs,
                    backup_urls=urls[1:],
                )
            )

        if not out:
            # 极端退路：bit_rate 为空时用顶层 play_addr（仍是无水印地址）
            urls = ((video.get("play_addr") or {}).get("url_list")) or []
            urls = [u for u in urls if u]
            if urls:
                out.append(
                    StreamInfo(
                        stream_id="dy-default",
                        url=urls[0],
                        label="默认清晰度",
                        vcodec="h264",
                        codec_family="h264",
                        is_muxed=True,
                        headers=hdrs,
                        backup_urls=urls[1:],
                    )
                )

        out.sort(key=lambda s: (s.height or 0, s.bandwidth or 0), reverse=True)
        return out


# --------------------------------------------------------------------------
# 兜底 1：外部解析 API（可配置，默认关闭）
# --------------------------------------------------------------------------


class DouyinApiExtractor(Extractor):
    name = "douyin-api"
    platform = "douyin"
    priority = 60
    label = "自定义解析接口"

    def match(self, url: str) -> bool:
        from app import config

        if not str(config.get("douyin_api_url") or "").strip():
            return False
        return "douyin.com" in url or "iesdouyin.com" in url

    async def resolve(self, url: str) -> list[MediaInfo]:
        from app import config

        tpl = str(config.get("douyin_api_url") or "").strip()
        if not tpl:
            raise ExtractError("未配置抖音解析接口")
        target = tpl.replace("{url}", url)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=httpx.Timeout(30.0, connect=15.0)
            ) as client:
                r = await client.get(target, headers={"User-Agent": DEFAULT_UA})
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise ExtractError(f"自定义解析接口调用失败：{exc}") from exc

        video_url = _dig(data, ("video_url", "url", "play_url", "nwm_video_url"))
        if not video_url:
            raise ExtractError("自定义解析接口未返回 video_url 字段")
        title = _dig(data, ("title", "desc")) or "抖音作品"
        author = _dig(data, ("author", "nickname")) or ""
        cover = _dig(data, ("cover", "cover_url")) or ""
        hdrs = _headers(_cookie())
        stream = StreamInfo(
            stream_id="api-default",
            url=str(video_url),
            label="接口返回清晰度",
            is_muxed=True,
            headers=hdrs,
        )
        return [
            MediaInfo(
                id=f"douyin-api:{abs(hash(video_url)) % 10**10}",
                platform="douyin",
                source_url=url,
                title=str(title),
                author=str(author),
                cover=str(cover),
                source=self.name,
                streams=[stream],
                warnings=["由自定义解析接口提供，清晰度与稳定性取决于该接口"],
            )
        ]


def _dig(data, keys):
    """在嵌套 dict 里按候选 key 找第一个非空标量。"""
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
            if isinstance(v, (int, float)) and k in ("title", "desc"):
                return v
        for v in data.values():
            got = _dig(v, keys)
            if got:
                return got
    elif isinstance(data, list):
        for v in data:
            got = _dig(v, keys)
            if got:
                return got
    return None


async def _selftest() -> None:  # pragma: no cover
    ex = DouyinExtractor()
    items = await ex.resolve("https://v.douyin.com/UBmfeB15e_k/")
    for it in items:
        print(it.title, "|", it.author, "|", it.duration)
        for s in it.streams:
            print("  ", s.stream_id, s.label, s.bandwidth)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_selftest())
