# -*- coding: utf-8 -*-
"""水印检测与去除。

背景（已实测确认，见 README）：
  B站会在转码时把「投稿水印」烧进画面——UP主昵称 + bilibili logo。
  实测同一稿件能拿到的 14 条码流（4 档清晰度 × 3 种编码 + durl + html5）**全部带水印**，
  且水印随分辨率等比缩放，即无法通过换码流规避。部分视频还有 UP主自加的移动水印。
  => 只能后期处理。

检测分两种模式：

  A. 全片模式（默认）
     固定水印「每帧都叠在同一位置」，因此它的**边缘**在时间轴上持续存在，
     而画面内容的边缘随镜头变化。做法：用 ffmpeg 的 edgedetect 取每帧边缘图，
     统计每个像素在多少帧里是边缘 -> 持续性图。持续性高的连通域就是固定水印。
     （早期版本用「帧 - 全片时域中值」，在多场景视频里会被镜头切换淹没，
      已废弃。）

  B. 单帧模式（传 t）
     针对会移动的水印：取指定时刻的一帧，用**近白掩膜**找出白色文字块，
     并按「块内亮度显著高于外圈」过滤掉画面本身的高亮内容。
     前端可让用户拖时间轴逐点取样，各点各带时间范围。

不依赖 numpy / PIL —— ffmpeg 只输出灰度 rawvideo，纯 Python 处理。
"""

from __future__ import annotations

import logging
import statistics
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.media.ffmpeg_locate import ffmpeg_path

log = logging.getLogger("vedio.watermark")

DELOGO_MARGIN = 2
SAMPLE_W = 384          # 采样宽度，越大越准也越慢
BOTTOM_EXCLUDE = 0.16   # 默认排除底部字幕带
MODES = ("auto", "delogo", "blur", "mosaic")


@dataclass
class WmBox:
    x: int
    y: int
    w: int
    h: int
    t0: float = 0.0
    t1: float = 0.0
    mode: str = "auto"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WmBox":
        return cls(
            x=int(d.get("x") or 0),
            y=int(d.get("y") or 0),
            w=max(2, int(d.get("w") or 2)),
            h=max(2, int(d.get("h") or 2)),
            t0=float(d.get("t0") or 0.0),
            t1=float(d.get("t1") or 0.0),
            mode=str(d.get("mode") or "auto"),
        )


# --------------------------------------------------------------------------
# 采样
# --------------------------------------------------------------------------


def _meta(path: str | Path) -> dict:
    from app.media import probe

    info = probe.summarize(path)
    v = info.get("video") or {}
    return {
        "duration": float(info.get("duration") or 0.0),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "exists": bool(info.get("exists")),
    }


def _run_raw(args: list[str], timeout: float = 900) -> bytes:
    exe = ffmpeg_path()
    if not exe:
        raise RuntimeError("未找到 ffmpeg")
    proc = subprocess.run(
        [exe, "-hide_banner", "-loglevel", "error", "-nostdin", *args],
        capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 执行失败：{(proc.stderr or b'')[:240]!r}")
    return proc.stdout


def _gray_frames(path, *, fps: float, out_w: int, edges: bool, t: float | None = None):
    """取灰度（或边缘）rawvideo 帧。返回 (frames, sw, sh)。"""
    vf = f"scale={out_w}:-2"
    if edges:
        vf += ",edgedetect=low=0.06:high=0.22"
    vf += ",format=gray"
    args: list[str] = []
    if t is not None:
        args += ["-ss", f"{max(0.0, t):.3f}"]
    args += ["-i", str(path)]
    if t is not None:
        args += ["-frames:v", "1"]
    else:
        vf = f"fps={fps:.6f}," + vf
    args += ["-vf", vf, "-f", "rawvideo", "-"]

    raw = _run_raw(args)
    meta = _meta(path)
    sw = out_w
    sh = int(round(meta["height"] * out_w / meta["width"])) if meta["width"] else 0
    if sh % 2:
        sh += 1
    fsize = sw * sh
    n = len(raw) // fsize
    return [raw[i * fsize:(i + 1) * fsize] for i in range(n)], sw, sh


# 近白判定阈值（亮度下限 / 饱和度上限）
# 阈值偏松：水印是半透明白字，压在彩色背景上时饱和度会被背景"染"上来。
# 靠后面的「同位置跨帧持续性」与形状校验来压制误报。
WHITE_LUMA = 150
WHITE_SAT = 72


def _white_mask(rgb: bytes, sw: int, sh: int) -> bytearray:
    """「近白 + 低饱和」掩膜。

    B站水印是白色半透明文字：压在暗背景上时它是画面里最亮的；
    压在彩色背景（如橙色天空）上时它是画面里最不饱和的。两个条件合起来
    能把水印从绝大多数画面内容里分出来。
    """
    mask = bytearray(sw * sh)
    for i in range(sw * sh):
        j = i * 3
        r = rgb[j]
        g = rgb[j + 1]
        b = rgb[j + 2]
        mx = r if r > g else g
        if b > mx:
            mx = b
        mn = r if r < g else g
        if b < mn:
            mn = b
        if mx - mn <= WHITE_SAT and (299 * r + 587 * g + 114 * b) // 1000 >= WHITE_LUMA:
            mask[i] = 1
    return mask


def _rgb_frames(path, *, fps: float, out_w: int, t: float | None = None):
    """取 rgb24 rawvideo 帧。返回 (frames, sw, sh)。

    注意：单帧定位把 -ss 放在 -i 之后（输出侧定位）。放在 -i 之前虽然更快，
    但对部分文件会配合 -frames:v 1 取出 0 帧。
    """
    args: list[str] = ["-i", str(path)]
    vf = f"scale={out_w}:-2,format=rgb24"
    if t is not None:
        args += ["-ss", f"{max(0.0, t):.3f}", "-frames:v", "1"]
    else:
        vf = f"fps={fps:.6f}," + vf
    args += ["-vf", vf, "-f", "rawvideo", "-"]

    raw = _run_raw(args)
    meta = _meta(path)
    sw = out_w
    sh = int(round(meta["height"] * out_w / meta["width"])) if meta["width"] else 0
    if sh % 2:
        sh += 1
    fsize = sw * sh * 3
    n = len(raw) // fsize
    return [raw[i * fsize:(i + 1) * fsize] for i in range(n)], sw, sh


# --------------------------------------------------------------------------
# 连通域
# --------------------------------------------------------------------------


def _components(mask, w: int, h: int, min_area: int, max_area: int):
    seen = bytearray(len(mask))
    out: list[tuple[int, int, int, int, int]] = []
    for start in range(len(mask)):
        if not mask[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        minx = maxx = start % w
        miny = maxy = start // w
        area = 0
        while stack:
            p = stack.pop()
            area += 1
            px, py = p % w, p // w
            if px < minx:
                minx = px
            elif px > maxx:
                maxx = px
            if py < miny:
                miny = py
            elif py > maxy:
                maxy = py
            if px > 0 and mask[p - 1] and not seen[p - 1]:
                seen[p - 1] = 1
                stack.append(p - 1)
            if px + 1 < w and mask[p + 1] and not seen[p + 1]:
                seen[p + 1] = 1
                stack.append(p + 1)
            if py > 0 and mask[p - w] and not seen[p - w]:
                seen[p - w] = 1
                stack.append(p - w)
            if py + 1 < h and mask[p + w] and not seen[p + w]:
                seen[p + w] = 1
                stack.append(p + w)
        if min_area <= area <= max_area:
            out.append((minx, miny, maxx - minx + 1, maxy - miny + 1, area))
    return out


def _merge(boxes, gap: int):
    """邻近小块合并（文字字形常被拆成多个连通域）。"""
    items = sorted(boxes, key=lambda b: -b[4])
    merged: list[list[int]] = []
    for x, y, w, h, a in items:
        hit = None
        for m in merged:
            if not (x > m[0] + m[2] + gap or m[0] > x + w + gap
                    or y > m[1] + m[3] + gap or m[1] > y + h + gap):
                hit = m
                break
        if hit is None:
            merged.append([x, y, w, h, a])
        else:
            nx, ny = min(hit[0], x), min(hit[1], y)
            nx2 = max(hit[0] + hit[2], x + w)
            ny2 = max(hit[1] + hit[3], y + h)
            hit[0], hit[1], hit[2], hit[3] = nx, ny, nx2 - nx, ny2 - ny
            hit[4] += a
    # 合并后再做一轮，处理链式相邻
    if len(merged) != len(items):
        return _merge([tuple(m) for m in merged], gap)  # type: ignore[arg-type]
    return [tuple(m) for m in merged]  # type: ignore[return-value]


def _texty(w: int, h: int, area: int, sw: int, sh: int = 0) -> bool:
    """水印是「一条文字」：偏扁长、有字距镂空，不是整片高亮也不是细线。"""
    if w < max(10, sw * 0.05) or h < 5:
        return False
    if h > sw * 0.30:
        return False
    ratio = w / max(1, h)
    if ratio < 0.8 or ratio > 26:
        return False
    fill = area / max(1, w * h)
    # 文字本身镂空度很高（0.1~0.5），但合并邻近区域后会升高，故上限放到 0.72
    if not (0.03 <= fill <= 0.82):
        return False
    if sh and (w * h) > (sw * sh) * 0.06:
        return False
    return True


# --------------------------------------------------------------------------
# 模式 A：全片固定水印（边缘时域持续性）
# --------------------------------------------------------------------------


def _detect_static(path, meta, samples: int, region: str) -> tuple[list[WmBox], str]:
    """全片固定水印：近白掩膜的「同位置跨帧持续性」。

    画面里的白色物体（云、衣服、灯、浅色墙）会移动或本身成片，因此某个像素在
    ≥50% 的采样帧里都「近白且低饱和」才是水印的强特征。

    阈值走三档自适应：先用严格档（又白又不带色），不成再逐级放宽。
    这样「白字压在近白墙上」靠严格档、「白字压在亮彩天空上」靠标准档，都能命中。
    三档命中计数在同一趟遍历里一起累计，不重复解码。
    """
    duration = meta["duration"]
    W, H = meta["width"], meta["height"]
    fps = samples / duration if duration > 0.5 else 1.0

    frames, sw, sh = _rgb_frames(path, fps=fps, out_w=SAMPLE_W)
    n = len(frames)
    if n < 6:
        return [], "采样帧数不足，无法做时域分析"

    profiles = (
        (200, 34, 0.55, "严格"),
        (150, 72, 0.50, "标准"),
        (132, 92, 0.45, "宽松"),
    )
    npix = sw * sh
    counters = [bytearray(npix) for _ in profiles]
    c1, c2, c3 = counters
    for f in frames:
        for i in range(npix):
            j = i * 3
            r = f[j]
            g = f[j + 1]
            b = f[j + 2]
            mx = r if r > g else g
            if b > mx:
                mx = b
            mn = r if r < g else g
            if b < mn:
                mn = b
            s = mx - mn
            l = (299 * r + 587 * g + 114 * b) // 1000
            if l >= 132 and s <= 92:
                c3[i] += 1
                if l >= 150 and s <= 72:
                    c2[i] += 1
                    if l >= 200 and s <= 34:
                        c1[i] += 1

    bottom_cut = int(sh * (1.0 - BOTTOM_EXCLUDE)) if region in ("top", "auto") else sh
    min_area = max(8, int(npix * 0.00030))
    max_area = int(npix * 0.05)
    sx, sy = W / sw, H / sh

    for (_luma, _sat, ratio, label), counter in zip(profiles, counters):
        need = max(3, int(n * ratio))
        mask = bytearray(1 if v >= need else 0 for v in counter)
        if BOTTOM_EXCLUDE > 0:
            for y in range(bottom_cut, sh):
                base = y * sw
                for x in range(sw):
                    mask[base + x] = 0

        comps = _components(mask, sw, sh, min_area, max_area)
        comps = _merge(comps, max(3, int(sw * 0.014)))
        comps = [c for c in comps if _texty(c[2], c[3], c[4], sw, sh)]

        boxes: list[WmBox] = []
        for x, y, w, h, _a in sorted(comps, key=lambda c: -c[4])[:3]:
            # 水印通常是「昵称 + logo」一长条，连通域常只框住其中一段，
            # 因此按自身尺寸再外扩一圈，避免残留半截文字。
            pad_x = max(6, int(w * sx * 0.20))
            pad_y = max(4, int(h * sy * 0.28))
            bx = max(0, int(x * sx) - pad_x)
            by = max(0, int(y * sy) - pad_y)
            bw = min(W - bx, int(w * sx) + pad_x * 2)
            bh = min(H - by, int(h * sy) + pad_y * 2)
            if bw >= 16 and bh >= 8:
                boxes.append(WmBox(x=bx, y=by, w=bw, h=bh, t0=0.0, t1=0.0))

        out: list[WmBox] = []
        for b in boxes:
            if any(abs(b.x - o.x) < 16 and abs(b.y - o.y) < 16 for o in out):
                continue
            out.append(b)
        if out:
            return out, (
                f"全片分析 {n} 帧（{label}阈值），检出 {len(out)} 处位置固定的水印"
            )

    return [], f"全片分析 {n} 帧，未发现位置固定的水印"


# --------------------------------------------------------------------------
# 模式 B：单帧白色文字块（供移动水印逐点取样）
# --------------------------------------------------------------------------


def _detect_single(path, meta, t: float, region: str) -> tuple[list[WmBox], str]:
    W, H = meta["width"], meta["height"]
    frames, sw, sh = _rgb_frames(path, fps=1.0, out_w=SAMPLE_W, t=t)
    if not frames:
        return [], f"无法读取 {t:.1f}s 处的画面"
    rgb = frames[0]
    mask = _white_mask(rgb, sw, sh)

    if region in ("top", "auto") and BOTTOM_EXCLUDE > 0:
        cut = int(sh * (1.0 - BOTTOM_EXCLUDE))
        for y in range(cut, sh):
            base = y * sw
            for x in range(sw):
                mask[base + x] = 0

    min_area = max(6, int(sw * sh * 0.00022))
    max_area = int(sw * sh * 0.035)
    comps = _components(mask, sw, sh, min_area, max_area)
    comps = _merge(comps, max(3, int(sw * 0.014)))
    comps = [c for c in comps if _texty(c[2], c[3], c[4], sw, sh)]

    # 块内饱和度应明显低于外圈（水印是白的，画面内容多半带色）
    keep = []
    for x, y, w, h, a in comps:
        pad = 5
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(sw, x + w + pad), min(sh, y + h + pad)
        inside, ring = [], []
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                j = (yy * sw + xx) * 3
                r, g, b = rgb[j], rgb[j + 1], rgb[j + 2]
                sat = max(r, g, b) - min(r, g, b)
                (inside if (x <= xx < x + w and y <= yy < y + h) else ring).append(sat)
        if not inside or not ring:
            continue
        if sum(ring) / len(ring) - sum(inside) / len(inside) >= 10:
            keep.append((x, y, w, h, a))

    # 水印通常是「昵称 + logo」一长条，检测往往只抓到其中一段。
    # 把处在同一水平带、水平方向相距不远的白色文字块并成一条。
    band: list[list[int]] = []
    for x, y, w, h, a in sorted(keep, key=lambda c: c[0]):
        hit = None
        for m in band:
            v_overlap = not (y > m[1] + m[3] or m[1] > y + h)
            h_gap = x - (m[0] + m[2])
            if v_overlap and h_gap <= int(sw * 0.14):
                hit = m
                break
        if hit is None:
            band.append([x, y, w, h, a])
        else:
            nx, ny = min(hit[0], x), min(hit[1], y)
            nx2 = max(hit[0] + hit[2], x + w)
            ny2 = max(hit[1] + hit[3], y + h)
            hit[0], hit[1], hit[2], hit[3] = nx, ny, nx2 - nx, ny2 - ny
            hit[4] += a

    sx, sy = W / sw, H / sh
    boxes = []
    for x, y, w, h, _a in sorted(band, key=lambda c: -c[4])[:4]:
        # 横向放宽得比纵向多：水印是「一长条文字」，检测常只抓到中间一段，
        # 宁可比实际宽一点（用户可以在画布上收窄），也不要留下半截字。
        pad_x = max(8, int(w * sx * 0.50))
        pad_y = max(4, int(h * sy * 0.30))
        bx = max(0, int(x * sx) - pad_x)
        by = max(0, int(y * sy) - pad_y)
        bw = min(W - bx, int(w * sx) + pad_x * 2)
        bh = min(H - by, int(h * sy) + pad_y * 2)
        if bw >= 16 and bh >= 8:
            boxes.append(
                WmBox(x=bx, y=by, w=bw, h=bh, t0=round(t, 2), t1=round(t, 2))
            )
    note = (
        f"{t:.1f}s 处检出 {len(boxes)} 个白色文字块（默认按 1.5s 窗口生效）"
        if boxes
        else f"{t:.1f}s 处未检出明显的白色文字覆盖层"
    )
    return boxes, note


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def detect(
    path: str | Path,
    *,
    samples: int = 48,
    region: str = "auto",
    t: float | None = None,
    mode: str = "auto",
    scan_step: float = 0.0,
    scan_max: int = 40,
) -> dict:
    import time

    t_start = time.time()
    meta = _meta(path)
    if not meta["exists"]:
        return {"ok": False, "note": f"文件不存在：{path}", "boxes": []}
    if not (meta["width"] and meta["height"] and meta["duration"] > 0.5):
        return {"ok": False, "note": "无法取得视频尺寸或时长", "boxes": []}

    try:
        if t is not None:
            boxes, note = _detect_single(path, meta, float(t), region)
        elif scan_step and scan_step > 0:
            boxes, note = _scan(path, meta, float(scan_step), region, scan_max)
        else:
            boxes, note = _detect_static(path, meta, max(12, min(120, samples)), region)
    except Exception as exc:  # noqa: BLE001
        log.exception("水印检测失败")
        return {"ok": False, "note": f"检测失败：{type(exc).__name__}: {exc}", "boxes": []}

    for b in boxes:
        b.mode = mode if mode in MODES else "auto"

    is_scan = bool(scan_step and scan_step > 0)
    return {
        "ok": True,
        "animated": is_scan or False,
        "duration": round(meta["duration"], 3),
        "width": meta["width"],
        "height": meta["height"],
        "single_frame": t is not None,
        "scanned": is_scan,
        "static_boxes": [b.to_dict() for b in boxes] if (t is None and not is_scan) else [],
        "segments": [b.to_dict() for b in boxes] if (t is not None or is_scan) else [],
        "boxes": [b.to_dict() for b in boxes],
        "tracks": [],
        "note": note + ("，可手动框选补充" if not boxes else ""),
        "seconds": round(time.time() - t_start, 2),
    }


def _scan(
    path, meta, step: float, region: str, max_points: int
) -> tuple[list[WmBox], str]:
    """定时扫描：在整条时间轴上每隔 step 秒做一次单帧检测。

    这是「移动水印」的半自动方案：不必手工拖时间轴逐点取样，
    程序按固定间隔扫一遍，把每个取样点的框配上覆盖该点的时间窗。
    扫描越密覆盖越好，代价是耗时线性增长。
    """
    duration = meta["duration"]
    step = max(2.0, min(60.0, step))
    points: list[float] = []
    tcur = step * 0.5
    while tcur < duration and len(points) < max_points:
        points.append(round(tcur, 2))
        tcur += step
    if not points:
        points = [duration * 0.5]

    half = step / 2.0
    boxes: list[WmBox] = []
    hits = 0
    for t in points:
        try:
            got, _note = _detect_single(path, meta, t, region)
        except Exception as exc:  # noqa: BLE001
            log.debug("扫描 t=%.1f 失败：%s", t, exc)
            continue
        if got:
            hits += 1
        for b in got[:2]:
            b.t0 = round(max(0.0, t - half), 2)
            b.t1 = round(min(duration, t + half), 2)
            boxes.append(b)

    if not boxes:
        return [], (
            f"定时扫描 {len(points)} 个时间点（间隔 {step:.0f}s），未检出白色文字覆盖层"
        )
    return boxes, (
        f"定时扫描 {len(points)} 个时间点（间隔 {step:.0f}s），"
        f"{hits} 个点有水印，共生成 {len(boxes)} 个限时区域"
    )


# --------------------------------------------------------------------------
# 滤镜生成
# --------------------------------------------------------------------------


def _clamp(b: WmBox, W: int, H: int) -> WmBox:
    x = max(0, min(int(b.x), W - 2))
    y = max(0, min(int(b.y), H - 2))
    w = max(2, min(int(b.w), W - x))
    h = max(2, min(int(b.h), H - y))
    return WmBox(x=x, y=y, w=w, h=h, t0=b.t0, t1=b.t1, mode=b.mode)


def _touch_edge(b: WmBox, W: int, H: int) -> bool:
    """贴边区域没有可供插值的邻域像素，delogo 会失败。"""
    return (
        b.x < DELOGO_MARGIN or b.y < DELOGO_MARGIN
        or b.x + b.w > W - DELOGO_MARGIN or b.y + b.h > H - DELOGO_MARGIN
    )


def _enable(b: WmBox, duration: float, offset: float = 0.0) -> str:
    """生成 enable 时间窗。

    重要：滤镜里的 t 是**输出时间轴**上的时间。只要 ffmpeg 用了 -ss
    （无论放在 -i 前还是后），时间戳都会被重置为从 0 开始。
    因此所有水印框的时间窗都必须减去起始偏移量 offset，否则
    「一边裁剪一边去水印」时窗口会整体错位（实测过：enable 66~82
    在 t=74 不生效，而 enable 0~10 反而生效）。
    """
    raw0 = max(0.0, float(b.t0 or 0.0))
    raw1 = float(b.t1 or 0.0)
    if raw1 and abs(raw1 - raw0) < 0.05:
        raw1 = raw0 + 1.5          # 单帧取样：默认给 1.5 秒窗口
    if raw1 <= 0:
        raw1 = duration or 0.0
    # 全程生效的框无需 enable
    if raw0 <= 0.0 and raw1 >= (duration or 1e9) - 0.05:
        return ""

    t0 = max(0.0, raw0 - offset)
    t1 = max(0.0, raw1 - offset)
    if t1 <= t0:
        t1 = t0 + 1.0
    return f":enable='between(t,{t0:.3f},{t1:.3f})'"


def build_vf(
    boxes,
    *,
    width: int,
    height: int,
    duration: float = 0.0,
    mode: str = "auto",
    strength: int = 3,
    tail: str = "",
    max_boxes: int = 90,
    time_offset: float = 0.0,
) -> str:
    """生成去水印滤镜链，可直接作为 ffmpeg 的 -vf 值。

    time_offset: 输出时间轴相对源时间轴的偏移（即 -ss 的起点）。
                 水印框的时间窗会整体减去它。
    """
    parsed = [b if isinstance(b, WmBox) else WmBox.from_dict(b) for b in (boxes or [])]
    if not parsed or not width or not height:
        return tail

    if len(parsed) > max_boxes:
        parsed.sort(key=lambda b: -(b.w * b.h))
        parsed = parsed[:max_boxes]
        log.warning("水印区域过多，已截断为 %d 个", max_boxes)

    parsed = [_clamp(b, width, height) for b in parsed]
    strength = max(1, min(5, int(strength or 3)))

    delogo_parts: list[str] = []
    graph_parts: list[str] = []
    current = "0:v"
    idx = 0

    for b in parsed:
        use = b.mode if b.mode in ("delogo", "blur", "mosaic") else ""
        if not use:
            # 贴边的必须用遮罩类方案；其余优先 delogo（画质最好）
            use = "blur" if _touch_edge(b, width, height) else "delogo"
        en = _enable(b, duration, time_offset)

        if use == "delogo":
            delogo_parts.append(f"delogo=x={b.x}:y={b.y}:w={b.w}:h={b.h}{en}")
            continue

        idx += 1
        base, patch, out = f"b{idx}", f"p{idx}", f"v{idx}"
        if use == "blur":
            pf = f"crop={b.w}:{b.h}:{b.x}:{b.y},boxblur={strength * 2}:1"
        else:
            factor = strength + 2
            dw, dh = max(2, b.w // factor), max(2, b.h // factor)
            pf = (
                f"crop={b.w}:{b.h}:{b.x}:{b.y},"
                f"scale={dw}:{dh}:flags=neighbor,"
                f"scale={b.w}:{b.h}:flags=neighbor"
            )
        graph_parts.append(f"[{current}]split=2[{base}][{patch}]")
        graph_parts.append(f"[{patch}]{pf}[{patch}m]")
        graph_parts.append(f"[{base}][{patch}m]overlay={b.x}:{b.y}{en}[{out}]")
        current = out

    if graph_parts:
        pre = ""
        if delogo_parts:
            pre = f"[0:v]{','.join(delogo_parts)}[vd0];"
            graph_parts[0] = graph_parts[0].replace("[0:v]", "[vd0]", 1)
        if tail:
            graph_parts.append(f"[{current}]{tail}")
        return pre + ";".join(graph_parts)

    chain = list(delogo_parts)
    if tail:
        chain.append(tail)
    return ",".join(chain) if chain else tail


def summary(cfg: dict | None, duration: float = 0.0) -> dict:
    cfg = cfg or {}
    boxes = cfg.get("boxes") or []
    if not cfg.get("enabled") or not boxes:
        return {"enabled": False, "count": 0, "note": ""}
    limited = sum(
        1 for b in boxes
        if float(b.get("t0") or 0) > 0 or float(b.get("t1") or 0) > 0
    )
    return {
        "enabled": True,
        "count": len(boxes),
        "limited": limited,
        "mode": str(cfg.get("mode") or "auto"),
        "strength": int(cfg.get("strength") or 3),
        "note": f"{len(boxes)} 个区域（{limited} 个为限时区域）",
    }
