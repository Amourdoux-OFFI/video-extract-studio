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


def _spawn_detached(args: list[str]) -> None:
    """脱离父进程启动，避免占用管道 / 随父进程退出。"""
    kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kw["creationflags"] = 0x00000008 | 0x00000200
    subprocess.Popen(args, **kw)


def open_browser_window(url: str) -> str:
    """以「应用窗口」形式打开界面。

    注意：**不要**给 Chrome 指定 --user-data-dir。实测在一台全新 profile 上
    Chrome 会因为首次运行初始化而启动失败，窗口根本不出现（现象是 profile
    目录建了、里面只有 Crashpad 文件、进程却不在）。复用默认 profile 时，
    Chrome 会把这个 --app 窗口挂到已有实例上，反而最稳。
    """
    exe = _find_browser()
    if exe:
        try:
            _spawn_detached([
                exe,
                f"--app={url}",
                "--window-size=1320,900",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate",
            ])
            log.info("已用 %s 以应用模式打开界面", exe)
            return exe
        except Exception as exc:  # noqa: BLE001
            log.warning("应用模式启动浏览器失败：%s", exc)

    import webbrowser

    log.info("改用系统默认浏览器打开")
    webbrowser.open(url)
    return "default-browser"


def launch(url: str, mode: str) -> str:
    """打开界面。返回**实际**生效的模式：'native' 或 'browser'。

    这个返回值很关键：原生模式下 webview.start() 会一直阻塞到用户关窗，
    返回即代表可以退出；而浏览器模式下窗口是独立进程，本函数立刻返回，
    调用方必须继续维持服务存活，否则窗口刚打开服务就被关掉了。
    """
    if mode in ("browser", "edge"):
        open_browser_window(url)
        return "browser"

    try:
        import webview

        t0 = time.time()
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
        # 秒退通常意味着窗口根本没显示出来（缺 WebView2 / 无桌面会话），
        # 这种情况要当作失败处理，转浏览器模式，而不是直接退出程序。
        if time.time() - t0 < 3.0:
            raise RuntimeError("原生窗口启动后立即返回（可能缺少 WebView2 运行时）")
        return "native"
    except Exception as exc:  # noqa: BLE001
        log.warning("pywebview 不可用，改用浏览器应用模式：%s", exc)
        say(f"  [提示] 原生窗口不可用（{exc}），已改用浏览器窗口")
        open_browser_window(url)
        return "browser"


def serve_until_exit(server, thread) -> None:
    """保持服务存活，直到控制台被关闭或收到 Ctrl+C。"""
    try:
        while thread.is_alive() and not server.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


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
        used = launch(url, mode)
        if used == "browser":
            # 浏览器窗口是独立进程，这里必须继续维持服务，
            # 否则会出现「窗口刚打开、服务已经关掉」的空白页。
            say("  界面已在浏览器窗口中打开；关闭本控制台窗口即可退出程序")
            serve_until_exit(server, thread)
    else:
        serve_until_exit(server, thread)

    server.should_exit = True
    thread.join(timeout=6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
