"""把淮阳区能搜到的 POI 写入池子（周口其余 5 县抖音无数据）。"""
from __future__ import annotations

import json
import shutil
import subprocess
import shutil as sh
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_filter import is_bad, is_target_poi  # noqa: E402

POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
exe = sh.which("yxer") or sh.which("yxer.cmd")
ACC = "6ae4185f9dc04c194bfd2bb8"
CITY = "周口市"
DISTRICT = "淮阳区"

found = {}
for q in ("淮阳", "淮阳商场", "淮阳广场", "淮阳购物中心", "淮阳区"):
    try:
        r = subprocess.run([exe, "query", "locations", ACC, "--query", q, "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=45)
        d = json.loads(r.stdout or "{}")
        items = ((d.get("data") or {}).get("dataList") or [])
    except Exception:
        continue
    for it in items:
        ai = ((it.get("raw") or {}).get("address_info")) or {}
        if str(ai.get("district") or "") != DISTRICT:
            continue
        name = str(it.get("yixiaoerName") or "")
        pid = str(it.get("yixiaoerId") or "")
        entry = {"name": name, "raw": it.get("raw") or {}}
        if not pid or is_bad(name) or not is_target_poi(entry):
            continue
        found[pid] = {"id": pid, "name": name, "raw": entry["raw"],
                      "desc": str(it.get("yixiaoerDesc") or "")}

if not found:
    print("淮阳区仍然 0 条")
    raise SystemExit(0)

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
bak = POOL.with_suffix(".json.bak-hy-%s" % stamp)
sh.copy2(POOL, bak)
pool = json.loads(POOL.read_text(encoding="utf-8"))
regions = pool.setdefault("regions", {})
key = CITY + DISTRICT
regions.setdefault(key, [])
exist = {str(p.get("id")) for p in regions[key]}
added = 0
for pid, p in found.items():
    if pid not in exist:
        regions[key].append(p)
        added += 1
POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
print("淮阳区新增 %d 条：%s" % (added, "、".join(p["name"] for p in found.values())))
print("池内 %d 键 / %d POI" % (len(regions), sum(len(v) for v in regions.values())))
print("备份:", bak)
