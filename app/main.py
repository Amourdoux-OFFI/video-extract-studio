# -*- coding: utf-8 -*-
"""应用入口：启动本地 HTTP 服务 + 套一层桌面窗口。

窗口三级降级：pywebview 原生窗口 -> Edge/Chrome 应用模式 -> 系统默认浏览器。
无论哪种方式，后端都是同一个本地 FastAPI 服务。
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app import config
from app.api.routes import router
from app.core.tasks import manager

log = logging.getLogger("vedio")

BANNER = r"""
  视频提取与剪辑工具  v{ver}
  工作目录 : {root}
  输出目录 : {out}
"""


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------


def _ensure_streams() -> None:
    """pythonw / --noconsole 下 sys.stdout 与 sys.stderr 都是 None。

    第三方库（argparse、uvicorn、logging）一旦往 None 上写就会直接崩，
    而且崩得悄无声息。这里统一兜底：stdout 丢弃，stderr 落到日志文件，
    以保证任何意外回溯都还能被追查。
    """
    import io

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        try:
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            sys.stderr = open(
                config.LOG_DIR / "stderr.log", "a", encoding="utf-8", buffering=1
            )
        except Exception:  # noqa: BLE001
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    # 让 print 的空格/换行在无缓冲场景下也安全
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if isinstance(stream, io.TextIOBase) and not stream.writable():
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def setup_logging(verbose: bool = False) -> None:
    config.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    # pythonw / --noconsole 下 sys.stdout 可能为 None，必须兜住
    stream = sys.stdout or sys.stderr
    if stream is not None:
        sh = logging.StreamHandler(stream)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    try:
        fh = logging.handlers.RotatingFileHandler(
            config.LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:  # noqa: BLE001
        pass

    if not root.handlers:
        root.addHandler(logging.NullHandler())

    for noisy in ("httpx", "httpcore", "uvicorn.access", "asyncio", "f2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def say(text: str = "") -> None:
    """安全打印：无控制台时静默跳过（但仍会写日志）。"""
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except Exception:  # noqa: BLE001
        pass


def free_port(preferred: int = 8756) -> int:
    for port in range(preferred, preferred + 60):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_ready(url: str, timeout: float = 25.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


# --------------------------------------------------------------------------
# FastAPI
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    manager.bind_loop(asyncio.get_running_loop())
    config.ensure_dirs()
    from app.media import ffmpeg_locate

    tools = ffmpeg_locate.probe_tools()
    if not tools["has_ffmpeg"]:
        log.warning("未检测到 ffmpeg，剪辑与合流功能将不可用")
    log.info("服务已就绪：ffmpeg=%s  nvenc=%s", tools["ffmpeg_path"] or "-", tools["has_nvenc"])
    yield
    log.info("服务关闭")


def create_app() -> FastAPI:
    app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)
    app.include_router(router)

    web = config.WEB_DIR
    if web.exists() and (web / "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(web), html=True), name="web")
    else:
        @app.get("/")
        async def placeholder():  # pragma: no cover
            from fastapi.responses import HTMLResponse

            return HTMLResponse(
                "<h2>前端资源缺失</h2><p>请确认 app/web/index.html 存在。</p>",
                status_code=200,
            )
    return app


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------


def _find_browser() -> str | None:
    import shutil

    for name in ("msedge", "chrome", "brave", "chromium"):
        p = shutil.which(name)
        if p:
            return p
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def open_browser_window(url: str) -> str:
    exe = _find_browser()
    if exe:
        try:
            profile = config.TEMP_DIR / "browser_profile"
            profile.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                [
                    exe,
                    f"--app={url}",
                    "--window-size=1320,900",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-features=Translate",
                ]
            )
            return exe
        except Exception as exc:  # noqa: BLE001
            log.warning("应用模式启动浏览器失败：%s", exc)

    import webbrowser

    webbrowser.open(url)
    return "default-browser"


def launch(url: str, mode: str) -> None:
    if mode in ("browser", "edge"):
        open_browser_window(url)
        return

    try:
        import webview

        window = webview.create_window(
            config.APP_NAME,
            url,
            width=1320,
            height=900,
            min_size=(1000, 660),
            background_color="#111318",
            text_select=True,
        )
        webview.start(debug=False)
        del window
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("pywebview 启动失败（改用浏览器应用模式）：%s", exc)
        open_browser_window(url)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=config.APP_NAME)
    ap.add_argument("--port", type=int, default=0, help="固定端口（默认自动挑选）")
    ap.add_argument(
        "--window", choices=["auto", "native", "browser"], default="auto",
        help="窗口模式：auto=优先原生窗口",
    )
    ap.add_argument("--no-window", action="store_true", help="只起服务，不开窗口")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    _ensure_streams()
    setup_logging(args.verbose)
    config.ensure_dirs()

    say(BANNER.format(ver=config.APP_VERSION, root=config.ROOT, out=config.out_dir()))

    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}/"

    import uvicorn

    uv_config = uvicorn.Config(
        create_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(uv_config)

    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    if not wait_ready(url + "api/health", timeout=30.0):
        log.error("服务启动失败，请查看 logs/app.log")
        return 2

    say(f"  服务地址 : {url}")
    say("  提示     : 关闭本窗口即可退出程序\n")

    if not args.no_window:
        mode = {"native": "native", "browser": "browser"}.get(args.window, "auto")
        launch(url, mode)
    else:
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    server.should_exit = True
    thread.join(timeout=6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
