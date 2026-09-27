# -*- coding: utf-8 -*-
"""解析器联调自测：用用户提供的 5 条真实链接验证两条链路。"""

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASTE = """2.05 复制打开抖音，看看【AI打工我享受的作品】一个不会聊天的AI，凭什么火遍硅谷？ 不写字、不画... https://v.douyin.com/UBmfeB15e_k/ :5pm 03/04 l@C.hb tRX:/     7.61 复制打开抖音，看看【具象波的作品】抽象哥整活出道，勇闯音乐圈丨CS音乐发展史（4） ... https://v.douyin.com/hmsAyc2hOP0/ bNj:/ :3pm F@U.yg 12/28
【驾考宝典-哔哩哔哩】 https://b23.tv/4xjGxJK      【东北泰坦尼克号-哔哩哔哩】 https://b23.tv/wD3ZkRM    【苦练半年的绕口令-哔哩哔哩】 https://b23.tv/Dmr98rV"""


def show(media) -> None:
    print(f"  [{media.platform}] {media.title[:52]}")
    print(f"      作者={media.author or '-'}  时长={media.duration:.1f}s  "
          f"kind={media.kind}  源={media.source}")
    for s in media.streams[:8]:
        mb = (s.bandwidth or 0) / 8 / 1024 / 1024 * media.duration
        print(f"      · {s.stream_id:<16} {s.label:<14} {s.resolution or '-':<10} "
              f"{s.codec_family or '-':<6} {(s.bandwidth or 0)//1000:>5} kbps  "
              f"约{mb:6.1f}MB  复用={s.is_muxed}")
    if len(media.streams) > 8:
        print(f"      · ... 另有 {len(media.streams) - 8} 档")
    for a in media.audios[:3]:
        print(f"      ♪ {a.stream_id:<16} {a.label:<14} {(a.bandwidth or 0)//1000:>5} kbps")
    for w in media.warnings:
        print(f"      ! {w}")


async def main() -> int:
    from app.core.link_extract import classify, extract_urls

    urls = extract_urls(PASTE)
    print(f"=== 抽链结果：共 {len(urls)} 条 ===")
    for u in urls:
        print(f"  {classify(u):<10} {u}")
    print()

    from app.extractors.registry import resolve_url

    total = len(urls)
    ok = 0
    for u in urls:
        print(f"=== 解析 {u}")
        t0 = time.time()
        items, errs = await resolve_url(u)
        dt = time.time() - t0
        if items:
            ok += 1
            print(f"  ✔ {dt:.1f}s 得到 {len(items)} 条")
            for m in items[:3]:
                show(m)
            if len(items) > 3:
                print(f"  ... 共 {len(items)} 条")
        else:
            print(f"  ✘ {dt:.1f}s 失败：")
            for e in errs:
                print(f"      {e.get('extractor')}: {e.get('error')}")
        print()

    print(f"=== 汇总：{ok}/{total} 条链接解析成功 ===")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
