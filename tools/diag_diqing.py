"""诊断迪庆州德钦县/维西县：看搜索能不能命中，以及 POI 的 city/district 实际值。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_filter import is_bad, is_target_poi  # noqa: E402
from app.services.publish.yxer_cli import run_yxer  # noqa: E402
from app.services.publish.yxer_imgtext import _extract_location_list  # noqa: E402

ACCS = {
    "修镯实录(丽江/迪庆/大理)": "6ad17a6dfb87f7a2682bfe9e",
    "老王全国实测(大理)": "6a72f808ee584439c1498a48",
}
QUERIES = ["德钦", "德钦县", "维西", "维西傈僳族自治县", "香格里拉",
           "迪庆", "商场", "超市", "步行街", "购物中心", "百货", "农贸市场"]

for label, acc in ACCS.items():
    print("=== %s ===" % label)
    for q in QUERIES:
        try:
            raw = run_yxer(["query", "locations", acc, "--query", q, "--json"],
                           timeout=60)
        except Exception as exc:
            print("  %-22s ERR %r" % (q, exc))
            continue
        items = _extract_location_list(raw) or []
        hits = []
        for it in items:
            ai = ((it.get("raw") or {}).get("address_info")) or {}
            c = str(ai.get("city") or "")
            d = str(ai.get("district") or "")
            if "迪庆" not in c and "德钦" not in d and "维西" not in d \
                    and "香格里拉" not in d:
                continue
            name = str(it.get("yixiaoerName") or "")
            e = {"name": name, "raw": it.get("raw") or {}}
            ok = (not is_bad(name)) and is_target_poi(e)
            hits.append("%s|%s|%s|%s" % (name[:16], c[:8], d[:8],
                                         "OK" if ok else "过滤"))
        print("  %-22s 返回%2d 条，迪庆相关 %d: %s"
              % (q, len(items), len(hits), "；".join(hits[:3]) or "无"))
    print()
