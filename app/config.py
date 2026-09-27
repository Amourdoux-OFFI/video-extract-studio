# -*- coding: utf-8 -*-
"""全局配置与路径。

打包成 exe 后：
  ROOT   = exe 所在目录（可写：下载/输出/配置/日志）
  BUNDLE = PyInstaller 解包目录（只读：静态资源、内置 ffmpeg）
开发运行时两者相同。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

APP_NAME = "视频提取与剪辑工具"
APP_VERSION = "1.0.0"


def _root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundle() -> Path:
    return Path(getattr(sys, "_MEIPASS", _root()))


ROOT: Path = _root()
BUNDLE: Path = _bundle()

DOWNLOAD_DIR = ROOT / "downloads"
OUTPUT_DIR = ROOT / "output"
TEMP_DIR = ROOT / "temp"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "config.json"
WEB_DIR = BUNDLE / "app" / "web"

DEFAULTS: dict = {
    "download_dir": str(DOWNLOAD_DIR),
    "output_dir": str(OUTPUT_DIR),
    "concurrency": 4,
    "chunk_size_mb": 4,
    "prefer_codec": "h264",
    "use_browser_cookie": True,
    "cookie_bilibili": "",
    "cookie_douyin": "",
    "use_nvenc": False,
    "ffmpeg_path": "",
    "filename_template": "{author} - {title}",
    "max_concurrent_tasks": 2,
    "http_proxy": "",
    # 可选的抖音外部解析兜底：填了才启用，支持 {url} 占位符
    "douyin_api_url": "",
}

_lock = threading.RLock()
_cache: dict | None = None


def ensure_dirs() -> None:
    for d in (DOWNLOAD_DIR, OUTPUT_DIR, TEMP_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            data = dict(DEFAULTS)
            if CONFIG_PATH.exists():
                try:
                    data.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001
                    pass
            _cache = data
        return dict(_cache)


def save(patch: dict) -> dict:
    global _cache
    with _lock:
        data = load()
        for k, v in (patch or {}).items():
            if k in DEFAULTS:
                data[k] = v
        _cache = data
        ensure_dirs()
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return dict(data)


def get(key: str):
    return load().get(key, DEFAULTS.get(key))


def out_dir() -> Path:
    p = Path(str(get("output_dir") or OUTPUT_DIR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def dl_dir() -> Path:
    p = Path(str(get("download_dir") or DOWNLOAD_DIR))
    p.mkdir(parents=True, exist_ok=True)
    return p
