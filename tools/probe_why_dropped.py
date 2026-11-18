"""查指定 POI 在池里的实际分类，判断被删是否正确。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_filter import is_bad, is_target_poi, poi_categories  # noqa: E402

POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
d = json.loads(POOL.read_text(encoding="utf-8"))
allp = [(k, p) for k, v in (d.get("regions") or {}).items() for p in v]

PROBES = ["天安中心广场", "衡辰商业广场", "上影广场", "徐家汇商圈", "万达广场",
          "PAC购物中心", "虹桥南丰城", "紫锦城", "露香园"]

for probe in PROBES:
    hit = [(k, p) for k, p in allp if probe in str(p.get("name"))]
    if not hit:
        print("%-12s （池里没有）" % probe)
        continue
    for k, p in hit[:1]:
        cats = poi_categories(p)
        keep = is_target_poi(p)
        bad = is_bad(p.get("name"))
        print("%-12s %-6s bad=%-5s cats=%s" % (
            str(p.get("name"))[:12], "保留" if keep else "删除", bad,
            " | ".join(cats) if cats else "(无分类,走名称)"))
