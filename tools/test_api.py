# -*- coding: utf-8 -*-
"""端到端 API 自测：走真实 HTTP 接口跑「解析 -> 下载 -> 合流」全链路。"""

import json
import sys
import time
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"
ROOT = Path(__file__).resolve().parent.parent

PASTE = """2.05 复制打开抖音，看看【AI打工我享受的作品】一个不会聊天的AI，凭什么火遍硅谷？ 不写字、不画... https://v.douyin.com/UBmfeB15e_k/ :5pm 03/04 l@C.hb tRX:/     7.61 复制打开抖音，看看【具象波的作品】抽象哥整活出道，勇闯音乐圈丨CS音乐发展史（4） ... https://v.douyin.com/hmsAyc2hOP0/ bNj:/ :3pm F@U.yg 12/28
【驾考宝典-哔哩哔哩】 https://b23.tv/4xjGxJK      【东北泰坦尼克号-哔哩哔哩】 https://b23.tv/wD3ZkRM    【苦练半年的绕口令-哔哩哔哩】 https://b23.tv/Dmr98rV"""


def line(t=""):
    print(t, flush=True)


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=120.0)
    fails = []

    line("=" * 78)
    line("[1] GET /api/health")
    r = c.get("/api/health")
    line(f"    http={r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        return 1

    line("\n[2] GET /api/settings")
    s = c.get("/api/settings").json()
    line(f"    ffmpeg      = {s.get('has_ffmpeg')}  {s.get('ffmpeg_path')}")
    line(f"    ffmpeg 版本 = {s.get('ffmpeg_version')}")
    line(f"    NVENC       = {s.get('has_nvenc')}")
    line(f"    输出目录    = {s.get('output_dir')}")
    line(f"    下载目录    = {s.get('download_dir')}")
    line(f"    并发/分片   = {s.get('concurrency')} / {s.get('chunk_size_mb')}MB")
    if not s.get("has_ffmpeg"):
        fails.append("ffmpeg 未就绪")

    line("\n[3] POST /api/parse（粘贴 5 条分享文本）")
    t0 = time.time()
    p = c.post("/api/parse", json={"text": PASTE}).json()
    line(f"    耗时 {time.time()-t0:.1f}s  找到 {p.get('found')} 条链接  "
         f"成功 {len(p.get('items') or [])} 条")
    for e in p.get("errors") or []:
        line(f"    ✘ {e.get('url')} -> {e.get('error')}")
        fails.append(f"解析失败 {e.get('url')}")
    for u in p.get("unrecognized") or []:
        line(f"    ? 未识别 {u}")

    items = p.get("items") or []
    if not items:
        line("    !! 没有解析到任何条目")
        return 1

    line("")
    for it in items:
        st = it.get("streams") or []
        au = it.get("audios") or []
        best = st[0] if st else {}
        line(f"    · [{it['platform_name']}] {it['title'][:40]}")
        line(f"        {it['author']} | {it['duration']:.1f}s | 共 {len(st)} 档视频 "
             f"{len(au)} 档音频 | 最优 {best.get('label')} {best.get('resolution')}")

    # ---- 选两条下载：一条抖音（复用流）、一条 B站（DASH 需合流） ----
    picks = []
    dy = next((i for i in items if i["platform"] == "douyin"), None)
    bl = next((i for i in items if i["platform"] == "bilibili"), None)
    for it in (dy, bl):
        if not it:
            continue
        st = it.get("streams") or []
        if not st:
            continue
        # 选最小的一档，省时间
        best = min(st, key=lambda x: x.get("bandwidth") or 0)
        au = (it.get("audios") or [None])[0]
        picks.append(
            {
                "media_id": it["id"],
                "stream_id": best["stream_id"],
                "audio_id": None if best.get("is_muxed") else (au or {}).get("stream_id"),
                "_label": f"{it['platform_name']} {it['title'][:24]} [{best.get('label')}]",
            }
        )

    line(f"\n[4] POST /api/download  共 {len(picks)} 条")
    body = [{"media_id": x["media_id"], "stream_id": x["stream_id"],
             "audio_id": x["audio_id"]} for x in picks]
    d = c.post("/api/download", json={"items": body}).json()
    for e in d.get("errors") or []:
        line(f"    ✘ {e}")
        fails.append(str(e))
    tids = d.get("task_ids") or []
    line(f"    task_ids = {tids}")
    for x in picks:
        line(f"      -> {x['_label']}")

    line("\n[5] 轮询任务进度（SSE 之外用 /api/tasks）")
    deadline = time.time() + 600
    seen = {}
    while time.time() < deadline:
        tasks = {t["id"]: t for t in c.get("/api/tasks").json()["tasks"]}
        running = 0
        for tid in tids:
            t = tasks.get(tid)
            if not t:
                continue
            key = (t["status"], round(t["percent"]))
            if seen.get(tid) != key:
                seen[tid] = key
                line(f"    {t['status']:<9} {t['percent']:6.1f}%  {t['phase']:<14} "
                     f"{t.get('message','')[:40]}  {t['title'][:24]}")
            if t["status"] in ("pending", "running"):
                running += 1
        if running == 0:
            break
        time.sleep(2)

    line("\n[6] 下载结果")
    tasks = {t["id"]: t for t in c.get("/api/tasks").json()["tasks"]}
    for tid in tids:
        t = tasks.get(tid)
        if not t:
            continue
        if t["status"] == "done":
            res = t.get("result") or {}
            line(f"    ✔ {res.get('name')}")
            line(f"       {res.get('size_text')}  时长={res.get('duration')}s  "
                 f"路径={res.get('path')}")
            v = res.get("video") or {}
            a = res.get("audio") or {}
            line(f"       视频 {v.get('codec')} {v.get('width')}x{v.get('height')} "
                 f"音频 {a.get('codec') or '无'}")
            for w in res.get("warnings") or []:
                line(f"       ! {w}")
            if not a.get("codec"):
                fails.append(f"{res.get('name')} 无音轨")
        else:
            line(f"    ✘ {t['status']} {t['title']}: {t.get('error')}")
            fails.append(f"下载失败 {t['title']}: {t.get('error')}")

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
