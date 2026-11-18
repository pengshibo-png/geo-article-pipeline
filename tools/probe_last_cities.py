"""诊断最后几个抓不到的区县：换关键词/换账号看能不能出数据。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_filter import is_bad, is_target_poi  # noqa: E402
from app.services.publish.yxer_cli import run_yxer  # noqa: E402
from app.services.publish.yxer_imgtext import _extract_location_list  # noqa: E402

TARGETS = [
    ("黄冈市", "团风县", ["6ae4185f9dc04c194bfd2bb8", "6ad17a6dfb87f7a2682bfe9e",
                       "6ad1789f736ba199556afab7"]),
    ("仙桃市", "仙桃市", ["6a72f6f7e2bf8d8e7cb3e3bd", "6a72f7195ef68d5087f51d7e"]),
]
QUERIES = ["{d}", "团风", "仙桃", "生活广场", "购物中心", "商场", "小区",
           "广场", "超市", "百货", "步行街", "商业街", "万达", "中百", "武商"]

for city, dist, accs in TARGETS:
    print("=== %s / %s ===" % (city, dist))
    hit = []
    for acc in accs:
        for q in QUERIES:
            qq = q.replace("{d}", dist)
            try:
                raw = run_yxer(["query", "locations", acc, "--query", qq,
                                "--json"], timeout=60)
            except Exception:
                continue
            items = _extract_location_list(raw) or []
            for it in items:
                ai = ((it.get("raw") or {}).get("address_info")) or {}
                c = str(ai.get("city") or "")
                d = str(ai.get("district") or "")
                if d != dist and c != city:
                    continue
                name = str(it.get("yixiaoerName") or "")
                e = {"name": name, "raw": it.get("raw") or {}}
                if is_bad(name) or not is_target_poi(e):
                    continue
                if name not in hit:
                    hit.append(name)
            if len(hit) >= 6:
                break
        if len(hit) >= 6:
            break
    print("   可用 %d 条: %s" % (len(hit), "、".join(hit[:6]) or "无"))
