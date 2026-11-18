"""采样统计 POI 的 poi_frontend_type 分类分布（用于按分类精确过滤）。"""
from __future__ import annotations

import ast
import json
import subprocess
import shutil
import time
from collections import Counter

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
ACC = "6ae3163e381ee67120da7dda"     # 天津号
REGIONS = ["和平区", "河西区", "南开区", "武清区", "蓟州区", "北辰区"]
QUERIES = ["{r}", "{r}商场", "{r}小区", "{r}广场"]


def cats_of(raw: dict) -> list[str]:
    """解析 poi_frontend_type → ['购物;百货商超;综合商场', ...]"""
    v = raw.get("poi_frontend_type")
    if not v:
        return []
    try:
        lst = ast.literal_eval(str(v))
        return [str(x.get("name")) for x in lst if isinstance(x, dict)]
    except Exception:
        return []


counter = Counter()
samples: dict[str, list[str]] = {}
for r in REGIONS:
    for tpl in QUERIES:
        q = tpl.replace("{r}", r)
        try:
            p = subprocess.run([exe, "query", "locations", ACC, "--query", q, "--json"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=45)
            d = json.loads(p.stdout or "{}")
            for it in ((d.get("data") or {}).get("dataList") or []):
                raw = it.get("raw") or {}
                name = str(it.get("yixiaoerName") or "")
                cs = cats_of(raw) or ["(无分类)"]
                for c in cs:
                    counter[c] += 1
                    samples.setdefault(c, [])
                    if len(samples[c]) < 3 and name not in samples[c]:
                        samples[c].append(name)
        except Exception:
            pass
        time.sleep(0.35)

print("分类分布（按出现次数）：\n")
for c, n in counter.most_common():
    print("%4d  %-42s %s" % (n, c[:40], "、".join(samples[c][:2])))
