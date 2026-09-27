@echo off
chcp 65001 >nul
title 视频提取与剪辑工具 (控制台)
cd /d "%~dp0"

set "PYC=%~dp0.venv\Scripts\python.exe"

if not exist "%PYC%" (
    echo [!] 未找到 .venv，请先安装依赖：pip install -r requirements.txt
    pause
    exit /b 1
)

"%PYC%" -m app.main --window browser %*
pause
