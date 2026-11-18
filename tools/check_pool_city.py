"""查看池内某城市的键与 POI 数（默认天津）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
city = sys.argv[1] if len(sys.argv) > 1 else "天津市"

d = json.loads(POOL.read_text(encoding="utf-8"))
regions = d.get("regions") or {}
hit = {k: v for k, v in regions.items() if k.startswith(city)}
print("池内总键 %d / 总 POI %d" % (len(regions), sum(len(v) for v in regions.values())))
print("%s 键 %d 个 / POI %d 条" % (city, len(hit), sum(len(v) for v in hit.values())))
for k in sorted(hit):
    names = [p.get("name") for p in hit[k][:3]]
    print("  %-14s %2d  %s" % (k, len(hit[k]), "、".join(str(n)[:14] for n in names)))
if not hit:
    print("  （还没有该城市的键 —— 抓取可能未完成或全部被过滤）")
