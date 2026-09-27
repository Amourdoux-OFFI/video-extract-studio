#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""获取 FFmpeg / FFprobe 可执行文件到 vendor/ffmpeg/。

仓库不包含这两个二进制（各约 79 MB，会把克隆变得极其痛苦），
首次克隆后跑一次本脚本即可。

用法：
    python tools/fetch_ffmpeg.py              # 自动选源
    python tools/fetch_ffmpeg.py --force      # 覆盖已存在的文件
    python tools/fetch_ffmpeg.py --check      # 只检查现状，不下载

只用标准库，不需要安装任何依赖。
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "ffmpeg"
VERSION = "b6.1.1"

# 按顺序尝试。npmmirror 是 ffmpeg-static 的官方镜像，国内直连通常 >10 MB/s。
SOURCES = [
    (
        "npmmirror（推荐，国内快）",
        "https://registry.npmmirror.com/-/binary/ffmpeg-static/{ver}/{name}.gz",
    ),
    (
        "gyan.dev（官方 Windows 构建）",
        "https://www.gyan.dev/ffmpeg/builds/{name}.gz",
    ),
]

TARGETS = {
    "ffmpeg": "ffmpeg-win32-x64",
    "ffprobe": "ffprobe-win32-x64",
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) video-extract-studio/fetch-ffmpeg"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = got / total * 100
                    sys.stdout.write(
                        f"\r      下载中 {pct:5.1f}%  {human(got)} / {human(total)}   "
                    )
                    sys.stdout.flush()
        sys.stdout.write("\r" + " " * 60 + "\r")
        tmp.replace(dest)


def gunzip(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)


def verify(exe: Path) -> tuple[bool, str]:
    """跑一次 -version 确认可执行，并回报构建信息。"""
    import subprocess

    try:
        out = subprocess.run(
            [str(exe), "-hide_banner", "-version"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        first = (out.stdout or out.stderr or "").strip().splitlines()
        return out.returncode == 0, first[0] if first else ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def main() -> int:
    ap = argparse.ArgumentParser(description="获取 FFmpeg / FFprobe")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    ap.add_argument("--check", action="store_true", help="只检查，不下载")
    args = ap.parse_args()

    DEST.mkdir(parents=True, exist_ok=True)
    ext = ".exe" if os.name == "nt" else ""

    print(f"目标目录：{DEST}\n")

    # ---------- 检查现状 ----------
    missing = []
    for name in TARGETS:
        exe = DEST / f"{name}{ext}"
        if exe.exists() and not args.force:
            ok, info = verify(exe)
            mark = "✔" if ok else "✘"
            print(f"  {mark} {exe.name:<14} {human(exe.stat().st_size):>10}  {info[:70]}")
            if not ok:
                missing.append(name)
        else:
            print(f"  · {name}{ext:<14} 不存在")
            missing.append(name)

    if args.check:
        print("\n（--check 模式，未做任何改动）")
        return 0 if not missing else 1

    if not missing:
        print("\n全部就绪，无需下载。")
        return 0

    # ---------- 下载 ----------
    last_err: Exception | None = None
    for name in missing:
        src_name = TARGETS[name]
        exe = DEST / f"{name}{ext}"
        print(f"\n[{name}]")
        done = False
        for label, tpl in SOURCES:
            url = tpl.format(ver=VERSION, name=src_name)
            print(f"    源：{label}")
            print(f"    {url}")
            gz = DEST / f"{src_name}.gz"
            try:
                download(url, gz)
                print(f"      解压 → {exe.name}")
                gunzip(gz, exe)
                gz.unlink(missing_ok=True)
                # Linux/macOS 需要可执行位
                if os.name != "nt":
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
                done = True
                break
            except urllib.error.HTTPError as exc:
                last_err = exc
                print(f"      ✘ HTTP {exc.code}，换下一个源")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                print(f"      ✘ {type(exc).__name__}: {exc}，换下一个源")
        if not done:
            print(f"    !! {name} 获取失败：{last_err}")

    # ---------- 复验 ----------
    print("\n结果：")
    bad = 0
    for name in TARGETS:
        exe = DEST / f"{name}{ext}"
        if exe.exists():
            ok, info = verify(exe)
            print(f"  {'✔' if ok else '✘'} {exe.name:<14} {human(exe.stat().st_size):>10}  {info[:70]}")
            bad += 0 if ok else 1
        else:
            print(f"  ✘ {name}{ext} 缺失")
            bad += 1

    if bad:
        print(
            "\n有文件未就绪。也可以自行下载后重命名放入 vendor/ffmpeg/，"
            "详见该目录下的 README.md。"
        )
        return 1

    print("\n完成。FFmpeg 为 GPLv3 构建，分发时请遵守 vendor/ffmpeg/README.md 中的义务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
