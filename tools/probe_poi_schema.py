"""查看抖音 POI 返回的完整字段，找有没有「分类/类型」可用于判断商场/小区。"""
from __future__ import annotations

import json
import subprocess
import shutil

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
ACC = "6ae3163e381ee67120da7dda"     # 天津号

r = subprocess.run([exe, "query", "locations", ACC, "--query", "和平区商场", "--json"],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=45)
d = json.loads(r.stdout or "{}")
items = ((d.get("data") or {}).get("dataList") or [])
print("返回 %d 条\n" % len(items))
if not items:
    raise SystemExit(0)

it = items[0]
print("=== 顶层字段 ===")
print(sorted(it.keys()))
print("\n=== raw 字段 ===")
raw = it.get("raw") or {}
print(sorted(raw.keys()))
print("\n=== 可能的分类字段 ===")
for k in raw:
    if any(x in k.lower() for x in ("categor", "type", "tag", "class", "kind", "industry")):
        print("  %s = %r" % (k, str(raw.get(k))[:200]))
print("\n=== 第一条完整内容（前 2500 字符）===")
print(json.dumps(it, ensure_ascii=False, indent=1)[:2500])
