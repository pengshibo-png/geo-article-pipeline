"""POI 池总览：按省份聚合（键名前缀匹配城市名）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
DISTRICTS = ROOT / "data" / "local" / "expanded_districts.json"

# 省份 → 该省所有地级市名（用于把池里的键归到省）
tree = json.loads(DISTRICTS.read_text(encoding="utf-8"))
CITY2PROV: dict[str, str] = {}
for prov, cities in tree.items():
    for city in cities:
        CITY2PROV[city] = prov
# 江浙沪老数据是「裸区县名」，单独归一类
OLD = "江浙沪（老数据，裸区县名）"

pool = json.loads(POOL.read_text(encoding="utf-8"))
regions = pool.get("regions") or {}

agg: dict[str, list[int]] = {}
for key, pois in regions.items():
    prov = None
    for city, p in CITY2PROV.items():
        if key.startswith(city):
            prov = p
            break
    prov = prov or OLD
    a = agg.setdefault(prov, [0, 0])
    a[0] += 1
    a[1] += len(pois)

print("POI 池总览：%d 键 / %d 条\n" % (len(regions), sum(len(v) for v in regions.values())))
print("%-22s %8s %10s" % ("省份 / 区域", "区县键", "POI 条数"))
print("-" * 44)
for prov, (k, n) in sorted(agg.items(), key=lambda x: -x[1][1]):
    print("%-22s %8d %10d" % (prov, k, n))
print("-" * 44)
print("%-22s %8d %10d" % ("合计", len(regions),
                          sum(len(v) for v in regions.values())))
