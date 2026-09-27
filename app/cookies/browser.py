# -*- coding: utf-8 -*-
"""从本机浏览器读取登录 Cookie（Chrome / Edge / Firefox）。"""

from __future__ import annotations

import logging

log = logging.getLogger("vedio.cookies")

_BROWSER_FUNCS = ("chrome", "edge", "firefox", "chromium", "brave")


def _collect(domain: str) -> str:
    try:
        import browser_cookie3 as bc3
    except Exception as exc:  # noqa: BLE001
        log.warning("browser_cookie3 不可用: %s", exc)
        return ""

    pairs: dict[str, str] = {}
    for name in _BROWSER_FUNCS:
        fn = getattr(bc3, name, None)
        if fn is None:
            continue
        try:
            jar = fn(domain_name=domain)
        except Exception:  # noqa: BLE001
            continue
        for c in jar:
            if c.name and c.value is not None and c.name not in pairs:
                pairs[c.name] = str(c.value)
    if not pairs:
        return ""
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def load_cookie(domain: str) -> str:
    """domain 形如 bilibili.com / douyin.com。失败返回空串。"""
    dom = (domain or "").lstrip(".")
    if not dom:
        return ""
    for candidate in (f".{dom}", dom):
        text = _collect(candidate)
        if text:
            log.info("已从浏览器读取 %s 的 Cookie（%d 字节）", dom, len(text))
            return text
    log.info("未从浏览器读到 %s 的 Cookie", dom)
    return ""


def bilibili_cookie() -> str:
    from app import config

    manual = str(config.get("cookie_bilibili") or "").strip()
    if manual:
        return manual
    if not config.get("use_browser_cookie"):
        return ""
    return load_cookie("bilibili.com")


def douyin_cookie() -> str:
    from app import config

    manual = str(config.get("cookie_douyin") or "").strip()
    if manual:
        return manual
    if not config.get("use_browser_cookie"):
        return ""
    return load_cookie("douyin.com")


def state() -> dict:
    """不真的读 Cookie，只看配置是否具备登录态。"""
    from app import config

    return {
        "cookie_bilibili": bool(str(config.get("cookie_bilibili") or "").strip()),
        "cookie_douyin": bool(str(config.get("cookie_douyin") or "").strip()),
        "use_browser_cookie": bool(config.get("use_browser_cookie")),
    }
