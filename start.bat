@echo off
chcp 65001 >nul
title 视频提取与剪辑工具
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\pythonw.exe"
set "PYC=%~dp0.venv\Scripts\python.exe"

if not exist "%PYC%" (
    echo.
    echo   [!] 未找到内置 Python 环境 .venv
    echo       请先运行：python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0vendor\ffmpeg\ffmpeg.exe" (
    echo.
    echo   [!] 未找到 vendor\ffmpeg\ffmpeg.exe
    echo       剪辑与合流功能不可用，但仍可解析与下载。
    echo.
)

echo   正在启动，请稍候...
start "" "%PY%" -m app.main
exit /b 0
