# -*- coding: utf-8 -*-
"""HTTP API：解析 / 下载 / 合流 / 剪辑 / 导入 / 进度推送。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import config
from app.core import downloader
from app.core.models import MediaInfo, human_size, safe_filename
from app.core.tasks import manager
from app.extractors import registry
from app.extractors.base import ExtractError
from app.extractors.local_file import scan_paths
from app.media import edit as editor
from app.media import ffmpeg_locate, mux, probe
from app.media import runner as ffrunner
from app.media import watermark as WM

log = logging.getLogger("vedio.api")
router = APIRouter(prefix="/api")

# 同时运行的下载任务上限（解析不受限）
_task_sem: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _task_sem
    if _task_sem is None:
        _task_sem = asyncio.Semaphore(int(config.get("max_concurrent_tasks") or 2))
    return _task_sem


def ok(data: dict | None = None, **kw) -> JSONResponse:
    payload = dict(data or {})
    payload.update(kw)
    return JSONResponse(payload)


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------


@router.get("/health")
async def health() -> JSONResponse:
    return ok(
        {
            "ok": True,
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "root": str(config.ROOT),
        }
    )


@router.get("/settings")
async def get_settings() -> JSONResponse:
    from app.cookies import state as cookie_state

    data = config.load()
    tools = ffmpeg_locate.probe_tools()
    return ok(
        {
            **data,
            **tools,
            **cookie_state(),
            "temp_dir": str(config.TEMP_DIR),
            "task_count": len(manager.all()),
        }
    )


@router.post("/settings")
async def set_settings(payload: dict = Body(default={})) -> JSONResponse:
    data = config.save(payload or {})
    if "ffmpeg_path" in (payload or {}):
        ffmpeg_locate.reset()
    return ok(data)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


@router.post("/parse")
async def parse(payload: dict = Body(...)) -> JSONResponse:
    text = str((payload or {}).get("text") or "")
    if not text.strip():
        return ok({"items": [], "unrecognized": [], "errors": [], "found": 0})

    registry.prune()
    task = manager.new("resolve", "解析链接")
    manager.start(task.id, "解析中")
    try:
        result = await registry.resolve_text(text)
    except Exception as exc:  # noqa: BLE001
        log.exception("解析失败")
        manager.fail(task.id, str(exc))
        return JSONResponse(
            {"items": [], "unrecognized": [], "errors": [{"url": "", "error": str(exc)}], "found": 0},
            status_code=200,
        )

    items = result["items"]
    manager.done(
        task.id,
        found=result["found"],
        success=len(items),
        failed=len(result["errors"]),
    )
    return ok(
        {
            "items": [m.to_dict() for m in items],
            "unrecognized": result["unrecognized"],
            "errors": result["errors"],
            "found": result["found"],
        }
    )


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------


def _unique_path(directory: Path, stem: str, ext: str = ".mp4") -> Path:
    p = directory / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = directory / f"{stem}({n}){ext}"
        n += 1
    return p


def _filename(media: MediaInfo, settings: dict) -> str:
    tpl = str(settings.get("filename_template") or "{author} - {title}")
    try:
        raw = tpl.format(
            author=media.author or "", title=media.title or "",
            platform=media.platform_name, id=media.id.split(":")[-1],
        )
    except Exception:  # noqa: BLE001
        raw = f"{media.author} - {media.title}"
    return safe_filename(raw, 80) or "未命名"


def _mapper(task_id: str, lo: float, hi: float, phase: str):
    """把 0-100 的下载进度映射到 [lo, hi] 区段。"""

    def cb(done: int, total: int, pct: float, speed: float, eta: float) -> None:
        manager.progress(
            task_id,
            lo + (hi - lo) * (pct / 100.0),
            phase=phase,
            speed=speed,
            eta=eta,
            message=(
                f"{human_size(done)} / {human_size(total)}" if total else human_size(done)
            ),
        )

    return cb


async def _download_one(task_id: str, media_id: str, stream_id: str, audio_id: str | None):
    settings = config.load()
    cancel = manager.cancel_event(task_id)
    out_dir = config.out_dir()
    tmp_dir = config.TEMP_DIR / task_id
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        media, stream, audio = registry.pick(media_id, stream_id, audio_id)
    except ExtractError as exc:
        manager.fail(task_id, str(exc))
        return

    manager.update(task_id, title=media.title[:80] or task_id)
    manager.start(task_id, "准备下载")
    dest = _unique_path(out_dir, _filename(media, settings))

    try:
        async with _sem():
            if cancel.is_set():
                raise downloader.Canceled()

            if stream.is_muxed or audio is None:
                await downloader.download(
                    stream.url,
                    dest,
                    headers=stream.headers,
                    backup_urls=stream.backup_urls,
                    on_progress=_mapper(task_id, 0.0, 92.0, "下载视频"),
                    connections=int(settings.get("concurrency") or 4),
                    chunk_mb=int(settings.get("chunk_size_mb") or 4),
                    cancel=cancel,
                    proxy=str(settings.get("http_proxy") or "") or None,
                )
            else:
                vpath = tmp_dir / "video.m4s"
                apath = tmp_dir / "audio.m4s"
                await downloader.download(
                    stream.url,
                    vpath,
                    headers=stream.headers,
                    backup_urls=stream.backup_urls,
                    on_progress=_mapper(task_id, 0.0, 62.0, "下载视频轨"),
                    connections=int(settings.get("concurrency") or 4),
                    chunk_mb=int(settings.get("chunk_size_mb") or 4),
                    cancel=cancel,
                    proxy=str(settings.get("http_proxy") or "") or None,
                )
                await downloader.download(
                    audio.url,
                    apath,
                    headers=audio.headers,
                    backup_urls=audio.backup_urls,
                    on_progress=_mapper(task_id, 62.0, 88.0, "下载音频轨"),
                    connections=min(4, int(settings.get("concurrency") or 4)),
                    chunk_mb=int(settings.get("chunk_size_mb") or 4),
                    cancel=cancel,
                    proxy=str(settings.get("http_proxy") or "") or None,
                )
                manager.progress(task_id, 90.0, phase="合流为 mp4", speed=0.0, eta=0.0)
                await asyncio.to_thread(
                    mux.mux, vpath, apath, dest, duration=media.duration
                )
                for f in (vpath, apath):
                    f.unlink(missing_ok=True)

            manager.progress(task_id, 94.0, phase="校验输出", speed=0.0, eta=0.0)
            info = await asyncio.to_thread(probe.summarize, dest)
            warns = list(media.warnings)
            if not (info.get("video") or {}).get("codec"):
                warns.append("输出文件未检测到视频流，可能下载不完整")
            if not (info.get("audio") or {}).get("codec") and not stream.is_audio_only:
                warns.append("输出文件未检测到音频流")

            manager.done(
                task_id,
                path=str(dest),
                name=dest.name,
                size=info.get("size", 0),
                size_text=info.get("size_text", ""),
                duration=info.get("duration", 0),
                video=info.get("video"),
                audio=info.get("audio"),
                platform=media.platform_name,
                title=media.title,
                warnings=warns,
            )
    except downloader.Canceled:
        dest.unlink(missing_ok=True)
        manager.update(task_id, status="canceled", phase="已取消", speed=0.0, eta=0.0)
    except Exception as exc:  # noqa: BLE001
        log.exception("下载失败")
        dest.unlink(missing_ok=True)
        manager.fail(task_id, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if tmp_dir.exists() and not any(tmp_dir.iterdir()):
                tmp_dir.rmdir()
        except Exception:  # noqa: BLE001
            pass


@router.post("/download")
async def download(payload: dict = Body(...)) -> JSONResponse:
    items = (payload or {}).get("items") or []
    if not items:
        return ok({"task_ids": [], "errors": [{"error": "没有选中任何条目"}]})

    task_ids: list[str] = []
    errors: list[dict] = []
    for it in items:
        media_id = str(it.get("media_id") or "")
        stream_id = str(it.get("stream_id") or "")
        audio_id = it.get("audio_id") or None
        media = registry.get(media_id)
        if media is None:
            errors.append({"media_id": media_id, "error": "解析结果已过期，请重新解析"})
            continue
        task = manager.new("download", media.title[:60] or "下载")
        task_ids.append(task.id)
        asyncio.create_task(_download_one(task.id, media_id, stream_id, audio_id))

    return ok({"task_ids": task_ids, "errors": errors})


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------


@router.get("/tasks")
async def tasks() -> JSONResponse:
    return ok({"tasks": manager.all()})


@router.post("/cancel")
async def cancel(payload: dict = Body(default={})) -> JSONResponse:
    tid = str((payload or {}).get("task_id") or "")
    if (payload or {}).get("all"):
        return ok({"ok": True, "canceled": manager.cancel_all()})
    return ok({"ok": manager.cancel(tid) if tid else False})


@router.post("/clear")
async def clear(payload: dict = Body(default={})) -> JSONResponse:
    body = payload or {}
    return ok(
        {
            "ok": True,
            "cleared": manager.clear(
                task_id=body.get("task_id"), finished_only=bool(body.get("finished_only"))
            ),
        }
    )


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    queue = manager.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            yield "data: " + json.dumps(
                {"type": "hello", "tasks": manager.all()}, ensure_ascii=False
            ) + "\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                except asyncio.TimeoutError:
                    yield "data: " + json.dumps({"type": "ping"}) + "\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------
# 本地导入
# --------------------------------------------------------------------------


def _pick_files() -> list[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"系统文件对话框不可用：{exc}") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass
    try:
        paths = filedialog.askopenfilenames(
            title="选择视频文件（可多选）",
            filetypes=[
                ("视频文件", "*.mp4 *.mkv *.mov *.avi *.flv *.wmv *.webm *.m4v "
                            "*.ts *.mts *.m2ts *.mpg *.mpeg *.3gp *.rmvb *.vob"),
                ("全部文件", "*.*"),
            ],
        )
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    return [str(p) for p in (paths or [])]


@router.post("/import/pick")
async def import_pick() -> JSONResponse:
    try:
        paths = await asyncio.to_thread(_pick_files)
    except Exception as exc:  # noqa: BLE001
        return ok({"items": [], "errors": [{"error": str(exc)}]})
    if not paths:
        return ok({"items": [], "errors": []})
    items, errors = await asyncio.to_thread(scan_paths, paths)
    for m in items:
        registry.put(m)
    return ok({"items": [m.to_dict() for m in items], "errors": errors})


@router.post("/import/paths")
async def import_paths(payload: dict = Body(default={})) -> JSONResponse:
    paths = [str(p) for p in ((payload or {}).get("paths") or []) if str(p).strip()]
    if not paths:
        return ok({"items": [], "errors": [{"error": "没有提供路径"}]})
    items, errors = await asyncio.to_thread(scan_paths, paths)
    for m in items:
        registry.put(m)
    return ok({"items": [m.to_dict() for m in items], "errors": errors})


@router.get("/probe")
async def probe_file(path: str) -> JSONResponse:
    p = Path(unquote(path))
    if not p.exists():
        return JSONResponse({"error": f"文件不存在：{p}"}, status_code=200)
    media = await asyncio.to_thread(probe.to_media_info, p)
    registry.put(media)
    return ok({"media": media.to_dict()})


# --------------------------------------------------------------------------
# 剪辑 / 压缩
# --------------------------------------------------------------------------


def _resolve_local(body: dict) -> str:
    path = str(body.get("path") or "").strip()
    if not path:
        media_id = str(body.get("media_id") or "")
        media = registry.get(media_id) if media_id else None
        if media and media.local_path:
            path = media.local_path
    if not path:
        raise ExtractError("未指定要处理的文件")
    p = Path(path)
    if not p.exists():
        raise ExtractError(f"文件不存在：{path}")
    return str(p)


@router.post("/estimate")
async def estimate(payload: dict = Body(...)) -> JSONResponse:
    body = payload or {}
    try:
        src = await asyncio.to_thread(_resolve_local, body)
        plan = await asyncio.to_thread(
            editor.build_plan, src, body.get("trim"), body.get("compress"),
            body.get("watermark"),
        )
    except Exception as exc:  # noqa: BLE001
        return ok({"ok": False, "note": str(exc)})
    est = dict(plan.get("estimate") or {})
    est["ok"] = bool(est.get("ok"))
    est["src_size_text"] = human_size(est.get("src_size") or 0)
    est["est_size_text"] = human_size(est.get("est_size") or 0)
    est["trim_note"] = (plan.get("trim") or {}).get("note", "")
    est["video_mode"] = plan.get("video_mode")
    est["out_duration"] = plan.get("out_duration")
    est["watermark"] = plan.get("wm_info") or {"enabled": False}
    if est["watermark"].get("enabled"):
        est["note"] = (est.get("note") or "") + "；已启用去水印，将重新编码"
    return ok(est)


async def _run_edit(task_id: str, body: dict) -> None:
    cancel = manager.cancel_event(task_id)
    try:
        src = await asyncio.to_thread(_resolve_local, body)
        manager.update(task_id, title=Path(src).stem[:60])
        manager.start(task_id, "分析源文件")

        def on_progress(pct: float, phase: str, speed: float) -> None:
            manager.progress(task_id, pct, phase=phase, speed=0.0)

        result = await asyncio.to_thread(
            editor.run_edit,
            src,
            str(config.out_dir()),
            trim=body.get("trim"),
            compress=body.get("compress"),
            watermark=body.get("watermark"),
            on_progress=on_progress,
            cancel=cancel,
        )
        if cancel.is_set():
            manager.update(task_id, status="canceled", phase="已取消")
            return
        manager.done(task_id, **result)
    except ffrunner.Canceled:
        manager.update(task_id, status="canceled", phase="已取消")
    except Exception as exc:  # noqa: BLE001
        log.exception("剪辑失败")
        manager.fail(task_id, f"{type(exc).__name__}: {exc}")


@router.post("/edit")
async def edit(payload: dict = Body(...)) -> JSONResponse:
    body = payload or {}
    try:
        src = await asyncio.to_thread(_resolve_local, body)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    task = manager.new("edit", Path(src).stem[:60] or "剪辑")
    manager.update(task_id=task.id, phase="排队中")
    asyncio.create_task(_run_edit(task.id, body))
    return ok({"task_id": task.id})


@router.get("/keyframes")
async def keyframes(path: str) -> JSONResponse:
    p = Path(unquote(path))
    if not p.exists():
        return JSONResponse({"error": "文件不存在"}, status_code=200)
    try:
        data = await asyncio.to_thread(probe.keyframes, p)
    except Exception as exc:  # noqa: BLE001
        return ok({"duration": 0.0, "keyframes": [], "error": str(exc)})
    # 只回传 UI 需要的粒度
    return ok(data)


# --------------------------------------------------------------------------
# 去水印：预览帧 + 自动检测
# --------------------------------------------------------------------------


def _render_frame(path: str, t: float, out_w: int, boxes: list) -> bytes:
    """渲染一帧 PNG；boxes 非空时先套上去水印滤镜。"""
    exe = ffmpeg_locate.ffmpeg_path()
    if not exe:
        raise RuntimeError("未找到 ffmpeg")

    info = probe.summarize(path)
    v = info.get("video") or {}
    src_w, src_h = int(v.get("width") or 0), int(v.get("height") or 0)
    dur = float(info.get("duration") or 0)

    tail = f"scale={out_w}:-2"
    vf = tail
    if boxes and src_w and src_h:
        # -ss 会重置输出时间轴，水印时间窗要按 t 偏移，否则预览会取错段
        vf = WM.build_vf(
            boxes, width=src_w, height=src_h, duration=dur,
            mode="auto", strength=3, tail=tail, time_offset=max(0.0, t),
        )

    def attempt(seek_after_input: bool) -> bytes:
        head = [exe, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if not seek_after_input:
            head += ["-ss", f"{max(0.0, t):.3f}"]
        cmd = head + ["-i", path]
        if seek_after_input:
            cmd += ["-ss", f"{max(0.0, t):.3f}"]
        cmd += ["-frames:v", "1", "-vf", vf, "-f", "image2", "-c:v", "png", "-"]
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"取帧失败：{(proc.stderr or b'')[:200]!r}")
        return proc.stdout

    png = b""
    try:
        png = attempt(False)      # 输入侧快速定位
    except Exception:  # noqa: BLE001
        png = b""
    if not png:
        png = attempt(True)       # 退路：输出侧定位（慢但更稳）
    if not png:
        raise RuntimeError("取帧失败：该时间点没有可用画面")
    return png


@router.get("/frame")
async def frame(
    path: str, t: float = 0.0, w: int = 960, boxes: str = ""
) -> Response:
    p = Path(unquote(path))
    if not p.exists():
        return JSONResponse({"error": f"文件不存在：{p}"}, status_code=404)
    out_w = max(160, min(1920, int(w or 960)))
    box_list: list = []
    if boxes:
        try:
            parsed = json.loads(unquote(boxes))
            if isinstance(parsed, list):
                box_list = parsed
        except Exception:  # noqa: BLE001
            box_list = []
    try:
        png = await asyncio.to_thread(_render_frame, str(p), float(t or 0.0), out_w, box_list)
    except Exception as exc:  # noqa: BLE001
        log.warning("取帧失败：%s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    return Response(
        content=png, media_type="image/png",
        headers={"Cache-Control": "no-store", "Content-Length": str(len(png))},
    )


@router.post("/watermark/detect")
async def watermark_detect(payload: dict = Body(default={})) -> JSONResponse:
    body = payload or {}
    try:
        path = await asyncio.to_thread(_resolve_local, body)
    except Exception as exc:  # noqa: BLE001
        return ok({"ok": False, "note": str(exc), "boxes": []})

    raw_t = body.get("t")
    t_val: float | None = None
    if raw_t is not None and str(raw_t) != "":
        try:
            t_val = float(raw_t)
        except (TypeError, ValueError):
            t_val = None

    try:
        res = await asyncio.to_thread(
            WM.detect,
            path,
            samples=int(body.get("samples") or 60),
            t=t_val,
            mode=str(body.get("mode") or "auto"),
            scan_step=float(body.get("scan_step") or 0.0),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("水印检测失败")
        return ok({"ok": False, "note": f"检测失败：{exc}", "boxes": []})
    return ok(res)


# --------------------------------------------------------------------------
# 文件访问 / 系统操作
# --------------------------------------------------------------------------


def _iter_range(path: Path, start: int, end: int, chunk: int = 1 << 20):
    with path.open("rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


@router.get("/file")
async def get_file(request: Request, path: str) -> Response:
    p = Path(unquote(path))
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "文件不存在"}, status_code=404)

    size = p.stat().st_size
    ext = p.suffix.lower().lstrip(".")
    mime = {
        "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm",
        "mkv": "video/x-matroska", "mov": "video/quicktime",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif",
    }.get(ext, "application/octet-stream")

    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    rng = request.headers.get("range")
    if rng and rng.startswith("bytes="):
        try:
            spec = rng.split("=", 1)[1].split(",")[0]
            a, _, b = spec.partition("-")
            start = int(a) if a else 0
            end = int(b) if b else size - 1
            start = max(0, start)
            end = min(size - 1, end)
            if start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        except Exception:  # noqa: BLE001
            start, end = 0, size - 1
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(
            _iter_range(p, start, end), status_code=206, media_type=mime, headers=headers
        )

    headers["Content-Length"] = str(size)
    return StreamingResponse(_iter_range(p, 0, size - 1), media_type=mime, headers=headers)


@router.post("/reveal")
async def reveal(payload: dict = Body(default={})) -> JSONResponse:
    path = str((payload or {}).get("path") or "")
    p = Path(path)
    if not p.exists():
        return ok({"ok": False, "error": "文件不存在"})
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(p)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
        return ok({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return ok({"ok": False, "error": str(exc)})


@router.post("/open")
async def open_file(payload: dict = Body(default={})) -> JSONResponse:
    path = str((payload or {}).get("path") or "")
    p = Path(path)
    if not p.exists():
        return ok({"ok": False, "error": "文件不存在"})
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return ok({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return ok({"ok": False, "error": str(exc)})


@router.post("/open-output")
async def open_output() -> JSONResponse:
    d = config.out_dir()
    try:
        if sys.platform == "win32":
            os.startfile(str(d))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
        return ok({"ok": True, "path": str(d)})
    except Exception as exc:  # noqa: BLE001
        return ok({"ok": False, "error": str(exc)})
