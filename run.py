# -*- coding: utf-8 -*-
"""打包入口。

放在项目根目录（而不是 app/main.py），这样 PyInstaller 的模块搜索根就是项目根，
`from app import ...` 才能被正确解析并打包进去。
"""

import multiprocessing
import sys
from pathlib import Path

# 源码直接运行时，把项目根加入 sys.path
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def _run() -> int:
    from app.main import main

    return main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(_run())
