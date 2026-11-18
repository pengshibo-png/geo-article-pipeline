"""诊断某个区县为什么抓不到 POI：看原始返回 + 被哪一环过滤。"""
from __future__ import annotations

import json
import subprocess
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_build import _address_matches  # noqa: E402
from app.services.douyin_poi_filter import is_bad, is_target_poi, poi_categories  # noqa: E402

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
ACC = "6a72f808ee584439c1498a48"      # 北京号
REGION = "海淀区"
QUERIES = ["海淀区购物中心", "海淀区商场", "海淀区", "海淀区广场", "中关村"]

for q in QUERIES:
    r = subprocess.run([exe, "query", "locations", ACC, "--query", q, "--json"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=45)
    d = json.loads(r.stdout or "{}")
    items = ((d.get("data") or {}).get("dataList") or [])
    print("=== 搜「%s」→ %d 条 ===" % (q, len(items)))
    for it in items[:6]:
        name = str(it.get("yixiaoerName") or "")
        raw = it.get("raw") or {}
        ai = raw.get("address_info") or {}
        entry = {"name": name, "raw": raw}
        reasons = []
        if not _address_matches(it, REGION):
            reasons.append("地址不含区名")
        if is_bad(name):
            reasons.append("黑名单")
        if not is_target_poi(entry):
            reasons.append("分类/名称不达标")
        cats = poi_categories(entry)
        print("   %-22s %s%s%s  cats=%s  %s"
              % (name[:20], ai.get("city"), ai.get("district"),
                 "", " | ".join(cats)[:40] if cats else "(无)",
                 "→ " + ",".join(reasons) if reasons else "→ 可收"))
    print()
