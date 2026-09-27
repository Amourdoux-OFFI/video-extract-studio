# -*- coding: utf-8 -*-
"""API 剪辑链路自测：estimate / edit / import / probe / keyframes / SSE。"""

import json
import sys
import threading
import time
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"
ROOT = Path(__file__).resolve().parent.parent

fails: list[str] = []


def line(t=""):
    print(t, flush=True)


def pick_src() -> str:
    out = ROOT / "output"
    files = [p for p in out.glob("*.mp4") if "剪辑测试" not in str(p) and p.stat().st_size > 200_000]
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return str(files[0]) if files else ""


def main() -> int:
    src = pick_src()
    if not src:
        line("!! 没有可用源文件")
        return 1
    line(f"源文件: {Path(src).name}  ({Path(src).stat().st_size/1048576:.2f} MB)")

    c = httpx.Client(base_url=BASE, timeout=300.0)

    # ---------- SSE 订阅（后台线程读事件） ----------
    events: list[dict] = []
    stop = threading.Event()

    def sse_reader():
        try:
            with httpx.Client(timeout=None) as sc:
                with sc.stream("GET", BASE + "/api/events") as r:
                    for raw in r.iter_lines():
                        if stop.is_set():
                            break
                        if not raw or not raw.startswith("data:"):
                            continue
                        try:
                            events.append(json.loads(raw[5:].strip()))
                        except Exception:  # noqa: BLE001
                            pass
        except Exception as exc:  # noqa: BLE001
            line(f"    (SSE 连接结束: {type(exc).__name__})")

    th = threading.Thread(target=sse_reader, daemon=True)
    th.start()
    time.sleep(1.5)
    line(f"\n[1] SSE 已连接，收到 {len(events)} 条事件（应含 hello）")
    kinds = [e.get("type") for e in events]
    if "hello" not in kinds:
        fails.append("SSE 未收到 hello 事件")
        line("    ✘ 未收到 hello")
    else:
        line("    ✔ 收到 hello")

    # ---------- 本地导入 ----------
    line("\n[2] POST /api/import/paths（本地导入）")
    r = c.post("/api/import/paths", json={"paths": [src]}).json()
    items = r.get("items") or []
    line(f"    items={len(items)} errors={r.get('errors')}")
    if not items:
        fails.append("本地导入失败")
    else:
        it = items[0]
        line(f"    ✔ {it['title'][:40]}  {it['duration']}s  "
             f"{it['streams'][0]['width']}x{it['streams'][0]['height']}")
        if it["platform"] != "local":
            fails.append("本地导入 platform 应为 local")

    # ---------- probe ----------
    line("\n[3] GET /api/probe")
    r = c.get("/api/probe", params={"path": src}).json()
    media = r.get("media") or {}
    line(f"    ✔ {media.get('title','')[:40]}  duration={media.get('duration')}")
    if not media:
        fails.append("probe 返回空")

    # ---------- keyframes ----------
    line("\n[4] GET /api/keyframes")
    kf = c.get("/api/keyframes", params={"path": src}).json()
    line(f"    关键帧 {len(kf.get('keyframes') or [])} 个，时长 {kf.get('duration')}s")
    if not (kf.get("keyframes") or []):
        line("    ! 未取到关键帧（不影响功能，会退化为帧精确模式）")

    # ---------- estimate ----------
    body = {
        "path": src,
        "trim": {"enabled": True, "start": 3.0, "end": 25.0, "mode": "smart"},
        "compress": {"enabled": True, "mode": "crf", "crf": 21, "preset": "fast",
                     "max_height": 0, "target_mb": 0, "use_nvenc": True, "mute": False},
    }
    line("\n[5] POST /api/estimate")
    est = c.post("/api/estimate", json=body).json()
    line(f"    ok={est.get('ok')} {est.get('src_size_text')} -> {est.get('est_size_text')} "
         f"({est.get('ratio')})  {est.get('note','')[:70]}")
    if not est.get("ok"):
        fails.append("estimate 失败")

    # ---------- edit ----------
    line("\n[6] POST /api/edit")
    ed = c.post("/api/edit", json=body).json()
    tid = ed.get("task_id")
    line(f"    task_id={tid}")
    if not tid:
        fails.append(f"edit 未返回 task_id: {ed}")
        stop.set()
        return 1

    deadline = time.time() + 300
    last = None
    while time.time() < deadline:
        tasks = {t["id"]: t for t in c.get("/api/tasks").json()["tasks"]}
        t = tasks.get(tid)
        if not t:
            time.sleep(1)
            continue
        key = (t["status"], round(t["percent"]))
        if key != last:
            last = key
            line(f"    {t['status']:<9} {t['percent']:6.1f}%  {t['phase']}")
        if t["status"] in ("done", "error", "canceled"):
            break
        time.sleep(1.5)

    stop.set()
    time.sleep(0.5)

    tasks = {t["id"]: t for t in c.get("/api/tasks").json()["tasks"]}
    t = tasks.get(tid) or {}
    if t.get("status") == "done":
        res = t.get("result") or {}
        line(f"    ✔ {res.get('name')}")
        line(f"       {res.get('src_size_text')} -> {res.get('size_text')} "
             f"(省 {res.get('saved_text')})  耗时 {res.get('seconds')}s")
        line(f"       {res.get('path')}")
        if not Path(res.get("path", "")).exists():
            fails.append("输出文件不存在")
    else:
        line(f"    ✘ {t.get('status')} {t.get('error')}")
        fails.append(f"edit 失败：{t.get('error')}")

    # ---------- SSE 事件核对 ----------
    task_events = [e for e in events if e.get("type") == "task" and
                   (e.get("task") or {}).get("id") == tid]
    line(f"\n[7] SSE 收到该任务的进度事件 {len(task_events)} 条")
    if not task_events:
        fails.append("SSE 未推送任务进度")
    else:
        first = task_events[0]["task"]
        final = task_events[-1]["task"]
        line(f"    首条: {first['status']} {first['percent']}%")
        line(f"    末条: {final['status']} {final['percent']}%")
        if final["status"] != "done":
            fails.append("SSE 末条不是 done")

    line("\n" + "=" * 70)
    if fails:
        line(f"结果：{len(fails)} 项失败")
        for f in fails:
            line(f"  - {f}")
        return 1
    line("结果：全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
