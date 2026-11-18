"""用商场通用词搜索（不带区名），看能否覆盖到缺失的区县。"""
from __future__ import annotations

import json
import subprocess
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_filter import is_bad, is_target_poi, poi_categories  # noqa: E402

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
ACC = sys.argv[1] if len(sys.argv) > 1 else "6a72f808ee584439c1498a48"   # 北京号
QUERIES = ["购物中心", "百货", "广场", "商场", "超市", "生活广场"]

dist_counter = Counter()
kept = []
for q in QUERIES:
    try:
        r = subprocess.run([exe, "query", "locations", ACC, "--query", q, "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=45)
        d = json.loads(r.stdout or "{}")
        items = ((d.get("data") or {}).get("dataList") or [])
    except Exception as exc:
        print("  %s ERR %r" % (q, exc))
        continue
    print("=== 搜「%s」→ %d 条 ===" % (q, len(items)))
    for it in items:
        name = str(it.get("yixiaoerName") or "")
        ai = (it.get("raw") or {}).get("address_info") or {}
        dist = str(ai.get("district") or "")
        entry = {"name": name, "raw": it.get("raw") or {}}
        ok = (not is_bad(name)) and is_target_poi(entry)
        mark = "OK " if ok else "no "
        print("   %s%-20s %s%s  %s" % (mark, name[:18], ai.get("city"),
                                       dist, "|".join(poi_categories(entry))[:26]))
        if ok:
            dist_counter[dist] += 1
            kept.append((dist, name))
print("\n=== 可用 POI 的区县分布 ===")
for d, n in dist_counter.most_common():
    print("  %-10s %d" % (d, n))
