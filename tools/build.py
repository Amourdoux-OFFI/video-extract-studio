# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本：产出免安装的 Windows 成品。

产物结构：
    dist/VideoExtractor/
        VideoExtractor.exe      主程序
        启动.bat                双击启动
        使用说明.txt
        _internal/              运行时与内置资源（含 ffmpeg）
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "VideoExtractor"

HIDDEN = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.constants",
    "anyio._backends._asyncio",
    "app.main",
    "app.config",
    "app.api.routes",
    "app.extractors.registry",
    "app.extractors.douyin",
    "app.extractors.bilibili",
    "app.extractors.ytdlp_fallback",
    "app.extractors.local_file",
    "app.media.edit",
    "app.media.compress",
    "app.media.trim",
    "app.media.mux",
    "app.media.probe",
    "app.cookies.browser",
]

EXCLUDES = [
    # 注意：不要排除 setuptools / distutils / pip —— PyInstaller 自带的
    # hook-distutils.py 需要把 setuptools._distutils 别名成 distutils，
    # 排除后会抛 "already imported as ExcludedModule" 直接中断打包。
    "black", "pytest", "_pytest", "pytest_asyncio", "iniconfig", "pluggy",
    "matplotlib", "numpy", "scipy", "pandas", "PIL",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "notebook", "jupyter", "nbformat", "nbconvert",
]


def clean() -> None:
    for d in (DIST / APP_NAME, BUILD):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def build() -> int:
    try:
        import PyInstaller.__main__ as pyi
    except Exception as exc:  # noqa: BLE001
        print(f"!! 未安装 PyInstaller：{exc}")
        return 1

    web = ROOT / "app" / "web"
    ffdir = ROOT / "vendor" / "ffmpeg"
    if not (web / "index.html").exists():
        print("!! app/web/index.html 缺失")
        return 1
    if not (ffdir / "ffmpeg.exe").exists():
        print("!  警告：vendor/ffmpeg/ffmpeg.exe 缺失，剪辑与合流功能将不可用")

    args = [
        str(ROOT / "run.py"),
        "--name", APP_NAME,
        "--noconfirm", "--clean",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--paths", str(ROOT),
        "--add-data", f"{web};app/web",
        "--collect-all", "f2",
        "--collect-all", "webview",
        "--collect-submodules", "browser_cookie3",
        "--console",
    ]
    if (ffdir / "ffmpeg.exe").exists():
        args += ["--add-data", f"{ffdir};vendor/ffmpeg"]
    for h in HIDDEN:
        args += ["--hidden-import", h]
    for e in EXCLUDES:
        args += ["--exclude-module", e]

    print("PyInstaller 参数：")
    for i in range(0, len(args), 2):
        print("   ", " ".join(args[i:i + 2]))
    print("\n开始打包（约 2-6 分钟）...\n", flush=True)

    pyi.run(args)
    return 0


def postprocess() -> None:
    """补充启动脚本与说明。"""
    target = DIST / APP_NAME
    if not target.exists():
        return
    (target / "启动.bat").write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "title 视频提取与剪辑工具\r\n"
        "cd /d \"%~dp0\"\r\n"
        "start \"\" \"%~dp0VideoExtractor.exe\"\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    (target / "使用说明.txt").write_text(
        "视频提取与剪辑工具\n"
        "==================\n\n"
        "启动：双击 VideoExtractor.exe 或 启动.bat\n\n"
        "功能：\n"
        "  1. 解析下载：把抖音 / B站的分享文本整段粘贴进来，可一次多条，\n"
        "     自动识别链接，解析出无水印视频后选择清晰度下载为 mp4。\n"
        "  2. 本地导入：拖入或选择电脑上的视频文件。\n"
        "  3. 剪辑压缩：无损裁剪（关键帧吸附）/ 帧精确裁剪 / 保画质压缩，\n"
        "     支持 NVIDIA 硬件加速（NVENC）。\n\n"
        "输出目录：output\\（与主程序同级，首次运行自动创建）\n"
        "日志目录：logs\\\n\n"
        "说明：\n"
        "  * 程序完全在本机运行，不上传任何数据。\n"
        "  * 首次运行会在本目录生成 config.json 配置文件。\n"
        "  * 若要解锁 B站 1080P60 / 4K 等会员画质，请在「设置」中填入\n"
        "    浏览器里登录后的 B站 Cookie。\n\n"
        "免责声明：本工具仅供个人学习与研究使用，请勿用于任何商业用途或\n"
        "侵权行为。下载的内容版权归原作者所有，请遵守各平台的服务条款。\n",
        encoding="utf-8",
    )
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"\n打包完成：{target}")
    print(f"总体积：{size / 1048576:.1f} MB")


if __name__ == "__main__":
    clean()
    rc = build()
    if rc == 0:
        postprocess()
    sys.exit(rc)
