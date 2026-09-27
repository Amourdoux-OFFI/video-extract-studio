# -*- coding: utf-8 -*-
"""最终验收：成品 exe 上跑完整业务链路（解析 / 下载 / 剪辑 / 去水印 / SSE）。"""

import json
import sys
import threading
import time
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8830"
ROOT = Path(__file__).resolve().parent.parent

PASTE = """2.05 复制打开抖音，看看【AI打工我享受的作品】一个不会聊天的AI，凭什么火遍硅谷？ 不写字、不画... https://v.douyin.com/UBmfeB15e_k/ :5pm 03/04 l@C.hb tRX:/     7.61 复制打开抖音，看看【具象波的作品】抽象哥整活出道，勇闯音乐圈丨CS音乐发展史（4） ... https://v.douyin.com/hmsAyc2hOP0/ bNj:/ :3pm F@U.yg 12/28
【驾考宝典-哔哩哔哩】 https://b23.tv/4xjGxJK      【东北泰坦尼克号-哔哩哔哩】 https://b23.tv/wD3ZkRM    【苦练半年的绕口令-哔哩哔哩】 https://b23.tv/Dmr98rV"""

fails: list[str] = []


def line(t=""):
    print(t, flush=True)


def wait(c, tid, limit=600):
    end = time.time() + limit
    while time.time() < end:
        tasks = {x["id"]: x for x in c.get("/api/tasks").json()["tasks"]}
        t = tasks.get(tid)
        if t and t["status"] in ("done", "error", "canceled"):
            return t
        time.sleep(1.5)
    return {}


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=900)

    line("=" * 78)
    line(f"目标：{BASE}")
    h = c.get("/api/health").json()
    line(f"[0] health: {h.get('app')} v{h.get('version')}")
    line(f"    工作目录: {h.get('root')}")

    s = c.get("/api/settings").json()
    line(f"[1] ffmpeg={s.get('has_ffmpeg')}  NVENC={s.get('has_nvenc')}")
    line(f"    {s.get('ffmpeg_version')}")
    if not s.get("has_ffmpeg"):
        fails.append("ffmpeg 不可用")

    # SSE
    events: list[dict] = []
    stop = threading.Event()

    def reader():
        try:
            with httpx.Client(timeout=None) as sc:
                with sc.stream("GET", BASE + "/api/events") as r:
                    for raw in r.iter_lines():
                        if stop.is_set():
                            break
                        if raw.startswith("data:"):
                            try:
                                events.append(json.loads(raw[5:].strip()))
                            except Exception:  # noqa: BLE001
                                pass
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(1.5)
    line(f"[2] SSE events={len(events)} hello={'hello' in [e.get('type') for e in events]}")
    if "hello" not in [e.get("type") for e in events]:
        fails.append("SSE 未握手")

    # 解析
    t0 = time.time()
    p = c.post("/api/parse", json={"text": PASTE}).json()
    items = p.get("items") or []
    line(f"[3] 解析 {time.time()-t0:.0f}s  链接 {p.get('found')}  成功 {len(items)}")
    for it in items:
        st = it.get("streams") or []
        best = st[0] if st else {}
        line(f"      [{it['platform_name']}] {it['title'][:34]} | "
             f"{len(st)} 档 | 最优 {best.get('label')} {best.get('resolution')}")
    if len(items) != 5:
        fails.append(f"解析成功数 {len(items)} != 5")

    # 下载一条 B站（走 DASH 合流）
    bl = next((i for i in items if i["platform"] == "bilibili"), None)
    dy = next((i for i in items if i["platform"] == "douyin"), None)
    picks = []
    for it in (bl, dy):
        if not it:
            continue
        st = it.get("streams") or []
        if not st:
            continue
        b = min(st, key=lambda x: x.get("bandwidth") or 0)
        au = (it.get("audios") or [None])[0]
        picks.append({
            "media_id": it["id"], "stream_id": b["stream_id"],
            "audio_id": None if b.get("is_muxed") else (au or {}).get("stream_id"),
            "_t": f"{it['platform_name']} {b.get('label')}",
        })
    d = c.post("/api/download", json={"items": [
        {k: v for k, v in x.items() if not k.startswith("_")} for x in picks]}).json()
    line(f"[4] 下载任务 {len(d.get('task_ids') or [])} 个")
    outs = []
    for tid in d.get("task_ids") or []:
        t = wait(c, tid)
        if t.get("status") == "done":
            r = t.get("result") or {}
            line(f"      ✔ {r.get('name','')[:48]}  {r.get('size_text')}")
            outs.append(r.get("path"))
        else:
            line(f"      ✘ {t.get('status')} {t.get('error')}")
            fails.append(f"下载失败 {t.get('error')}")

    # 用下载到的文件做：去水印 + 裁剪 + CRF + NVENC
    if outs:
        src = outs[0]
        line(f"\n[5] 去水印检测: {Path(src).name[:40]}")
        det = c.post("/api/watermark/detect", json={"path": src, "samples": 48}).json()
        line(f"    ok={det.get('ok')} {det.get('note')}  ({det.get('seconds')}s)")
        boxes = det.get("boxes") or []
        for b in boxes:
            line(f"      x={b['x']} y={b['y']} w={b['w']} h={b['h']}")

        line("\n[6] 预览帧对比")
        f1 = c.get("/api/frame", params={"path": src, "t": 3, "w": 480})
        f2 = c.get("/api/frame", params={"path": src, "t": 3, "w": 480,
                                         "boxes": json.dumps(boxes)})
        line(f"    原始 {f1.status_code} {len(f1.content)}B  "
             f"去水印 {f2.status_code} {len(f2.content)}B")
        if f1.status_code != 200 or f2.status_code != 200:
            fails.append("预览帧接口异常")

        body = {
            "path": src,
            "trim": {"enabled": True, "start": 1.0, "end": 12.0, "mode": "smart"},
            "compress": {"enabled": True, "mode": "crf", "crf": 21,
                         "preset": "fast", "use_nvenc": True},
            "watermark": {"enabled": bool(boxes), "mode": "auto",
                          "strength": 3, "boxes": boxes},
        }
        est = c.post("/api/estimate", json=body).json()
        line(f"\n[7] 预估 ok={est.get('ok')}  {est.get('src_size_text')} -> "
             f"{est.get('est_size_text')}")
        line(f"    {est.get('note','')[:120]}")

        ed = c.post("/api/edit", json=body).json()
        tid = ed.get("task_id")
        t = wait(c, tid)
        line(f"\n[8] 剪辑 status={t.get('status')} {t.get('error') or ''}")
        if t.get("status") == "done":
            r = t.get("result") or {}
            line(f"    {r.get('name')}")
            line(f"    {r.get('src_size_text')} -> {r.get('size_text')}  耗时 {r.get('seconds')}s")
            line(f"    {r.get('path')}")
            if not Path(r.get("path", "")).exists():
                fails.append("剪辑输出不存在")
        else:
            fails.append(f"剪辑失败：{t.get('error')}")

    stop.set()
    time.sleep(0.5)
    te = [e for e in events if e.get("type") == "task"]
    line(f"\n[9] SSE 共推送 {len(te)} 条任务事件")
    if not te:
        fails.append("SSE 无任务推送")

    line("\n" + "=" * 78)
    if fails:
        line(f"结果：{len(fails)} 项失败")
        for f in fails:
            line(f"  - {f}")
        return 1
    line("结果：全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
