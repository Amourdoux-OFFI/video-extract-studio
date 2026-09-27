# -*- coding: utf-8 -*-
"""哔哩哔哩解析器。

链路：
    b23.tv 短链 --302--> www.bilibili.com/video/BVxxxx
    -> x/web-interface/view        取标题/作者/封面/时长/分P
    -> x/player/playurl (fnval=4048)  取 DASH 视频轨 + 音频轨
    -> 前端选流 -> 下载（必须带 Referer）-> ffmpeg -c copy 合流为 mp4

已验证事实（本机实测）：
  * nav 接口无需登录即可返回 WBI 密钥
  * playurl 免签名即可返回完整 DASH
  * DASH 每档含 AVC(7)/HEVC(12)/AV1(13) 三套编码，默认优先 AVC 保兼容
  * CDN 分片必须带 Referer，否则 403
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from urllib.parse import urlencode

import httpx

from app.core.link_extract import direct_avid, direct_bvid, page_index
from app.core.models import (
    MediaInfo,
    StreamInfo,
    codec_family_of,
    quality_label_bili,
)
from app.cookies import bilibili_cookie
from app.extractors.base import DEFAULT_UA, ExtractError, Extractor

log = logging.getLogger("vedio.bilibili")

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYURL = "https://api.bilibili.com/x/player/playurl"
API_WBI_PLAYURL = "https://api.bilibili.com/x/player/wbi/playurl"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"

AUDIO_LABELS = {
    30216: "64K",
    30232: "132K",
    30280: "192K",
    30250: "杜比全景声",
    30251: "无损 FLAC",
}

MAX_PAGES = 10  # 一次最多展开多少个分P

_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in _MIXIN_TAB if i < len(orig))[:32]


def enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """WBI 签名（w_rid + wts）。"""
    mixin = _mixin_key(img_key + sub_key)
    p = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in params.items()}
    p["wts"] = int(time.time())
    p = dict(sorted(p.items()))
    query = urlencode(p)
    p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return p


def _headers(referer: str, cookie: str = "") -> dict:
    h = {
        "User-Agent": DEFAULT_UA,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


class BilibiliExtractor(Extractor):
    name = "bilibili-api"
    platform = "bilibili"
    priority = 10
    label = "B站官方接口"

    def match(self, url: str) -> bool:
        return "bilibili.com" in url or "b23.tv" in url

    async def resolve(self, url: str) -> list[MediaInfo]:
        cookie = bilibili_cookie()
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(25.0, connect=15.0),
            headers={"User-Agent": DEFAULT_UA},
        ) as client:
            bvid, avid = await self._to_id(client, url, cookie)
            view = await self._get_json(
                client, API_VIEW, {"bvid": bvid} if bvid else {"aid": avid}, cookie, url
            )
            if view.get("code") != 0:
                raise ExtractError(
                    f"B站 view 接口返回错误：{view.get('message') or view.get('code')}"
                )
            data = view.get("data") or {}
            return await self._build(client, data, url, cookie)

    # ---------- 步骤 ----------

    async def _to_id(self, client: httpx.AsyncClient, url: str, cookie: str) -> tuple[str, str]:
        bvid = direct_bvid(url)
        if bvid:
            return bvid, ""
        avid = direct_avid(url)
        if avid:
            return "", avid
        # 短链：跟随跳转后从最终地址里取
        try:
            r = await self._get_with_retry(
                client, url, headers=_headers("https://www.bilibili.com/", cookie)
            )
            final = str(r.url)
        except ExtractError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExtractError(
                f"短链跳转失败（{type(exc).__name__}）：{exc}"
            ) from exc
        bvid = direct_bvid(final) or ""
        avid = direct_avid(final) or ""
        if not bvid and not avid:
            raise ExtractError(f"无法从短链解析出 BV 号：{final[:120]}")
        return bvid, avid

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        api: str,
        params: dict,
        cookie: str,
        referer: str,
    ) -> dict:
        try:
            r = await self._get_with_retry(
                client, api, params=params, headers=_headers(referer, cookie)
            )
            return r.json()
        except ExtractError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExtractError(
                f"请求 {api} 失败（{type(exc).__name__}）：{exc}"
            ) from exc

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        attempts: int = 3,
    ) -> httpx.Response:
        last: Exception | None = None
        for i in range(max(1, attempts)):
            try:
                return await client.get(url, params=params, headers=headers)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if i + 1 < attempts:
                    await asyncio.sleep(0.5 * (i + 1))
        raise ExtractError(f"网络请求失败（{type(last).__name__}）：{last}")

    async def _wbi_keys(
        self, client: httpx.AsyncClient, cookie: str
    ) -> tuple[str, str] | None:
        """取 WBI 签名密钥（无需登录）。"""
        try:
            r = await self._get_with_retry(
                client, API_NAV, headers=_headers("https://www.bilibili.com/", cookie)
            )
            wbi = ((r.json() or {}).get("data") or {}).get("wbi_img") or {}
            img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
            sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
            if img and sub:
                return img, sub
        except Exception as exc:  # noqa: BLE001
            log.debug("获取 WBI 密钥失败：%s", exc)
        return None

    async def _playurl(
        self, client: httpx.AsyncClient, bvid: str, cid: int, cookie: str, referer: str
    ) -> dict:
        """返回 dash 节点（不是外层 data）。

        关键参数（本机实测对比 10 种组合后确定）：
          * 免签名 + try_look=1 + fnver=0  -> 画质 id [80,64,32,16] 即 1920x976 起
          * 免签名（不带 try_look）        -> 只剩 [32,16]，被限到 480P
          * WBI 签名（带不带 try_look 都一样）-> 只剩 [32,16]
        也就是说 try_look 才是解锁高码率档位的开关，与是否签名无关。
        """
        base = {
            "bvid": bvid,
            "cid": cid,
            "qn": 127,
            "fnval": 4048,
            "fnver": 0,
            "fourk": 1,
            "try_look": 1,
        }
        tried: list[str] = []

        # 主路：免签名 + try_look（实测画质最高）
        try:
            r = await self._get_with_retry(
                client, API_PLAYURL, params=base, headers=_headers(referer, cookie)
            )
            j = r.json()
            dash = (j.get("data") or {}).get("dash")
            if j.get("code") == 0 and dash and dash.get("video"):
                return dash
            tried.append(f"try_look code={j.get('code')} {j.get('message')}")
        except Exception as exc:  # noqa: BLE001
            tried.append(f"try_look {type(exc).__name__}: {exc}")

        # 退路一：免签名但不带 try_look
        try:
            plain = {k: v for k, v in base.items() if k != "try_look"}
            r = await self._get_with_retry(
                client, API_PLAYURL, params=plain, headers=_headers(referer, cookie)
            )
            j = r.json()
            dash = (j.get("data") or {}).get("dash")
            if j.get("code") == 0 and dash and dash.get("video"):
                return dash
            tried.append(f"免签名 code={j.get('code')} {j.get('message')}")
        except Exception as exc:  # noqa: BLE001
            tried.append(f"免签名 {type(exc).__name__}: {exc}")

        # 退路二：WBI 签名
        keys = await self._wbi_keys(client, cookie)
        if keys:
            try:
                signed = enc_wbi(dict(base), keys[0], keys[1])
                r = await self._get_with_retry(
                    client, API_WBI_PLAYURL, params=signed,
                    headers=_headers(referer, cookie),
                )
                j = r.json()
                dash = (j.get("data") or {}).get("dash")
                if j.get("code") == 0 and dash and dash.get("video"):
                    return dash
                tried.append(f"WBI code={j.get('code')} {j.get('message')}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"WBI {type(exc).__name__}: {exc}")

        raise ExtractError("B站取流失败：" + "；".join(tried))

    # ---------- 组装 ----------

    async def _build(
        self, client: httpx.AsyncClient, data: dict, src_url: str, cookie: str
    ) -> list[MediaInfo]:
        bvid = str(data.get("bvid") or "")
        title = str(data.get("title") or "").strip()
        author = str((data.get("owner") or {}).get("name") or "").strip()
        cover = str(data.get("pic") or "")
        duration = float(data.get("duration") or 0)
        pages = data.get("pages") or []
        if not pages:
            raise ExtractError("B站返回的数据里没有分P信息")

        want = page_index(src_url)
        if want > len(pages):
            want = 1
        chosen = [pages[want - 1]]
        extra_note = ""
        if len(pages) > 1 and "p=" not in src_url.lower():
            chosen = pages[:MAX_PAGES]
            extra_note = f"该稿件共 {len(pages)} P，已展开前 {len(chosen)} P"
            if len(pages) > MAX_PAGES:
                extra_note += f"（其余请用链接加 ?p=N 单独解析）"

        out: list[MediaInfo] = []
        for pg in chosen:
            cid = int(pg.get("cid") or 0)
            pno = int(pg.get("page") or 1)
            pg_title = str(pg.get("part") or title)
            pg_dur = float(pg.get("duration") or duration)
            referer = f"https://www.bilibili.com/video/{bvid}"
            if pno > 1:
                referer += f"?p={pno}"

            dash = await self._playurl(client, bvid, cid, cookie, referer)
            streams, audios, warns = self._streams(dash, bvid, pno, referer, cookie)
            if not streams:
                raise ExtractError(
                    f"未能从 DASH 数据中解析出视频轨（dash.video="
                    f"{len((dash or {}).get('video') or [])}）"
                )

            if extra_note:
                warns.append(extra_note)
            best = max(streams, key=lambda s: (s.height or 0, s.bandwidth or 0))
            # 竖屏视频的 height 会很大（如 480x852），按短边描述更符合习惯
            short = (
                min(best.width, best.height)
                if (best.width and best.height)
                else best.height
            )
            if not cookie and int(best.quality_id or 0) < 80:
                warns.append(
                    f"未登录：当前最高约 {short or '?'}P。"
                    "若该稿件有更高清晰度，可在「设置」里填入 B站 Cookie 后重试"
                )
            elif not cookie:
                warns.append(
                    f"未登录已取到 {best.label}；1080P60 / 4K / 8K 及杜比、无损音轨需大会员"
                )

            out.append(
                MediaInfo(
                    id=f"bilibili:{bvid}:p{pno}",
                    platform="bilibili",
                    source_url=src_url,
                    title=(pg_title if len(chosen) > 1 or pno > 1 else title) or pg_title,
                    author=author,
                    cover=cover,
                    duration=pg_dur,
                    kind="video",
                    source=self.name,
                    streams=streams,
                    audios=audios,
                    warnings=warns,
                    raw={"bvid": bvid, "cid": cid, "page": pno, "pages": len(pages)},
                )
            )
        return out

    def _streams(
        self, dash: dict, bvid: str, pno: int, referer: str, cookie: str
    ) -> tuple[list[StreamInfo], list[StreamInfo], list[str]]:
        hdrs = _headers(referer, cookie)
        warns: list[str] = []

        # 按画质 id 分组，每组保留最优编码（偏好可在设置里改）
        from app import config as app_config

        prefer = str(app_config.get("prefer_codec") or "h264")
        pref = [prefer] + [c for c in ("h264", "hevc", "av1", "vp9") if c != prefer]
        groups: dict[int, dict[str, dict]] = {}
        for v in dash.get("video") or []:
            qid = int(v.get("id") or 0)
            codecs = str(v.get("codecs") or "")
            fam = codec_family_of(codecs, int(v.get("codecid") or 0)) or "other"
            groups.setdefault(qid, {})
            cur = groups[qid].get(fam)
            if cur is None or int(v.get("bandwidth") or 0) > int(cur.get("bandwidth") or 0):
                groups[qid][fam] = v

        streams: list[StreamInfo] = []
        for qid, fams in groups.items():
            pick = None
            for f in pref:
                if f in fams:
                    pick = (f, fams[f])
                    break
            if pick is None:
                pick = next(iter(fams.items()))
            fam, v = pick
            try:
                fps = float(str(v.get("frameRate") or 0).split(".")[0] or 0)
            except ValueError:
                fps = 0.0
            h = int(v.get("height") or 0)
            w = int(v.get("width") or 0)
            label = quality_label_bili(qid, h, fps)
            streams.append(
                StreamInfo(
                    stream_id=f"v{qid}-{int(v.get('codecid') or 0)}",
                    url=str(v.get("baseUrl") or v.get("base_url") or ""),
                    label=label,
                    quality_id=qid,
                    width=w,
                    height=h,
                    fps=fps,
                    vcodec=str(v.get("codecs") or ""),
                    codec_family=fam,
                    bandwidth=int(v.get("bandwidth") or 0),
                    is_audio_only=False,
                    is_muxed=False,
                    headers=hdrs,
                    backup_urls=list(
                        v.get("backupUrl") or v.get("backup_url") or []
                    ),
                )
            )

        streams.sort(key=lambda s: (s.height or 0, s.bandwidth or 0), reverse=True)

        audios: list[StreamInfo] = []
        for a in dash.get("audio") or []:
            aid = int(a.get("id") or 0)
            audios.append(
                StreamInfo(
                    stream_id=f"a{aid}",
                    url=str(a.get("baseUrl") or a.get("base_url") or ""),
                    label=AUDIO_LABELS.get(aid, f"{aid}"),
                    quality_id=aid,
                    acodec=str(a.get("codecs") or ""),
                    codec_family=codec_family_of(str(a.get("codecs") or "")),
                    bandwidth=int(a.get("bandwidth") or 0),
                    is_audio_only=True,
                    is_muxed=False,
                    headers=hdrs,
                    backup_urls=list(a.get("backupUrl") or a.get("backup_url") or []),
                )
            )
        for special, key in (("dolby", "杜比全景声"), ("flac", "无损 FLAC")):
            node = (dash.get(special) or {}).get("audio")
            if isinstance(node, dict) and node.get("baseUrl"):
                audios.append(
                    StreamInfo(
                        stream_id=f"a-{special}",
                        url=str(node.get("baseUrl")),
                        label=key,
                        acodec=str(node.get("codecs") or ""),
                        bandwidth=int(node.get("bandwidth") or 0),
                        is_audio_only=True,
                        is_muxed=False,
                        headers=hdrs,
                        backup_urls=list(node.get("backupUrl") or []),
                    )
                )

        audios.sort(key=lambda s: s.bandwidth or 0, reverse=True)
        if not audios:
            warns.append("未取到独立音轨，输出可能无声")
        return streams, audios, warns


def extract_bvid(url: str) -> str:
    return direct_bvid(url) or ""


def short_id(url: str) -> str:
    m = re.search(r"b23\.tv/([A-Za-z0-9]+)", url)
    return m.group(1) if m else ""


async def _selftest() -> None:  # pragma: no cover
    ex = BilibiliExtractor()
    items = await ex.resolve("https://b23.tv/4xjGxJK")
    for it in items:
        print(it.title, it.author, it.duration)
        for s in it.streams:
            print("  ", s.stream_id, s.label, s.resolution, s.bandwidth)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_selftest())
