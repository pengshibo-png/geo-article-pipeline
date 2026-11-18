"""看搜「德钦」「维西」返回的具体 POI 与行政区字段。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.publish.yxer_cli import run_yxer  # noqa: E402
from app.services.publish.yxer_imgtext import _extract_location_list  # noqa: E402

ACC = "6ad17a6dfb87f7a2682bfe9e"
for q in ("德钦", "德钦县", "维西傈僳族自治县", "维西"):
    raw = run_yxer(["query", "locations", ACC, "--query", q, "--json"], timeout=60)
    items = _extract_location_list(raw) or []
    print("=== 搜「%s」返回 %d 条 ===" % (q, len(items)))
    for it in items:
        ai = ((it.get("raw") or {}).get("address_info")) or {}
        print("   %-24s %s%s%s" % (str(it.get("yixiaoerName"))[:22],
                                   ai.get("province"), ai.get("city"),
                                   ai.get("district")))
    print()
