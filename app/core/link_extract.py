# -*- coding: utf-8 -*-
"""从一大段脏文本里抽出可解析的视频链接。

输入示例（用户真实粘贴内容）：
    2.05 复制打开抖音，看看【AI打工我享受的作品】... https://v.douyin.com/UBmfeB15e_k/ :5pm 03/04 l@C.hb tRX:/
    【驾考宝典-哔哩哔哩】 https://b23.tv/4xjGxJK

策略：两段式匹配，绝不误吃垃圾串。
  1) 显式 http(s):// URL
  2) 无 scheme 的已知站点短链（v.douyin.com/xxx、b23.tv/xxx）
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# 已知站点的主机名（无 scheme 也能识别）
KNOWN_HOSTS = (
    "v.douyin.com",
    "www.douyin.com",
    "douyin.com",
    "www.iesdouyin.com",
    "iesdouyin.com",
    "b23.tv",
    "www.bilibili.com",
    "bilibili.com",
    "m.bilibili.com",
)

_HOST_ALT = "|".join(re.escape(h) for h in KNOWN_HOSTS)

# URL 里合法的普通字符；遇到空白或中日韩标点即停
_TAIL = r"[^\s\u3000-\u303f\u4e00-\u9fff\uff00-\uffef\u2018\u2019\u201c\u201d]+"

_EXPLICIT = re.compile(r"https?://" + _TAIL, re.IGNORECASE)
_BARE = re.compile(r"(?<![\w.\-/])((?:" + _HOST_ALT + r")/" + _TAIL + ")", re.IGNORECASE)

# 结尾常见的“粘连标点”
_TRAILING = " \t\r\n.,;:!?)]}>\"'，。；：！？）】》”、’"

# 明显不是资源页的路径
_SKIP_PATHS = ("/login", "/passport", "/faq", "/about", "/contact")


def _clean(raw: str) -> str:
    s = raw.strip().strip("\"'<>")
    while s and s[-1] in _TRAILING:
        s = s[:-1]
    return s


def _normalize(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except Exception:  # noqa: BLE001
        return url
    scheme = "https"
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def extract_urls(text: str) -> list[str]:
    """按出现顺序返回去重后的链接列表。"""
    if not text:
        return []

    found: list[tuple[int, str]] = []
    for m in _EXPLICIT.finditer(text):
        found.append((m.start(), m.group(0)))
    for m in _BARE.finditer(text):
        found.append((m.start(), m.group(1)))

    found.sort(key=lambda x: x[0])

    out: list[str] = []
    seen: set[str] = set()
    for _, raw in found:
        u = _clean(raw)
        if not u:
            continue
        n = _normalize(u)
        low = n.lower()
        if any(p in low for p in _SKIP_PATHS):
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(n)
    return out


_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_AWEME_RE = re.compile(r"/(?:video|note|slides)/(\d{6,})")
_DY_SHORT_RE = re.compile(r"v\.douyin\.com/([A-Za-z0-9_\-]+)")


def classify(url: str) -> str:
    """返回 douyin / bilibili / unknown。"""
    try:
        host = (urlsplit(url).netloc or "").lower().split(":")[0]
    except Exception:  # noqa: BLE001
        return "unknown"
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return "douyin"
    if host.endswith("bilibili.com") or host == "b23.tv":
        return "bilibili"
    return "unknown"


def direct_bvid(url: str) -> str | None:
    m = _BV_RE.search(url)
    return m.group(1) if m else None


def direct_avid(url: str) -> str | None:
    m = _AV_RE.search(url)
    return m.group(1) if m else None


def direct_aweme_id(url: str) -> str | None:
    m = _AWEME_RE.search(url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]modal_id=(\d{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]aweme_id=(\d{6,})", url)
    if m:
        return m.group(1)
    return None


def is_short_douyin(url: str) -> bool:
    return bool(_DY_SHORT_RE.search(url))


def page_index(url: str) -> int:
    """B 站多 P 链接里的 ?p=N。"""
    m = re.search(r"[?&]p=(\d+)", url)
    try:
        return max(1, int(m.group(1))) if m else 1
    except Exception:  # noqa: BLE001
        return 1
