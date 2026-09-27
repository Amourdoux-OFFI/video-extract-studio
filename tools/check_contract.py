# -*- coding: utf-8 -*-
"""前后端契约核对：把 app.js 里调用的接口与 FastAPI 实际路由逐一对齐。"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api.routes import router  # noqa: E402

JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8", errors="replace")
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8", errors="replace")

# 后端实际路由
backend = {}
for r in router.routes:
    p = getattr(r, "path", None)
    if not p:
        continue
    methods = {m.upper() for m in (getattr(r, "methods", None) or [])}
    methods.discard("HEAD")
    methods.discard("OPTIONS")
    backend[p] = methods

print(f"后端路由 {len(backend)} 条：")
for p in sorted(backend):
    print(f"   {','.join(sorted(backend[p])) or '-':<12} {p}")

# 前端调用的接口
calls = set()
for m in re.finditer(r"""['"`](/api/[A-Za-z0-9_\-/]*)['"`]""", JS):
    calls.add(m.group(1))
for m in re.finditer(r"""(?:api|fetch)\(\s*[`'"](/api/[A-Za-z0-9_\-/]*)""", JS):
    calls.add(m.group(1))
# 反引号模板里的
for m in re.finditer(r"`(/api/[A-Za-z0-9_\-/]+)", JS):
    calls.add(m.group(1))

print(f"\n前端调用的接口 {len(calls)} 个：")
missing = []
for c in sorted(calls):
    hit = c in backend
    if not hit:
        # 允许前缀匹配（例如 /api/file?path=）
        base = c.split("?")[0]
        hit = base in backend
    print(f"   {'✔' if hit else '✘'} {c}")
    if not hit:
        missing.append(c)

# 前端引用的元素 id 是否都在 HTML 里
ids_html = set(re.findall(r'id="([^"]+)"', HTML))
ids_js = set(re.findall(r"""getElementById\(\s*['"]([^'"]+)['"]""", JS))
ids_js |= set(re.findall(r"""querySelector\(\s*['"]#([A-Za-z0-9_\-]+)['"]""", JS))
orphan = sorted(ids_js - ids_html)

print(f"\nHTML 元素 id 共 {len(ids_html)} 个；JS 引用 {len(ids_js)} 个")
if orphan:
    print(f"   ✘ JS 引用了不存在的 id：{orphan}")
else:
    print("   ✔ JS 引用的 id 全部存在")

# 前端读取的任务字段 vs 后端产出的字段
task_fields = set(re.findall(r"""\btask\.([a-z_]+)""", JS))
task_fields |= set(re.findall(r"""t\.([a-z_]+)\b""", JS))
py_task = (ROOT / "app" / "core" / "tasks.py").read_text(encoding="utf-8")
declared = set(re.findall(r"^    ([a-z_]+):", py_task, re.M))
unknown = sorted(f for f in task_fields if f not in declared and len(f) > 2)
print(f"\nTask 字段：后端声明 {len(declared)} 个")
print(f"   前端用到但后端没声明的（可能是我漏看）：{unknown}")

print("\n" + "=" * 60)
if missing or orphan:
    print("结果：存在不一致 ✘")
    sys.exit(1)
print("结果：前后端契约一致 ✔")
