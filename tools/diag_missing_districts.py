"""列出各城市缺失的区县，并逐个搜索看返回什么（是否真没有商场）。"""
from __future__ import annotations

import json
import subprocess
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_build import _address_matches  # noqa: E402
from app.services.douyin_poi_filter import is_bad, is_target_poi, poi_categories  # noqa: E402

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
DISTRICTS = ROOT / "data" / "local" / "expanded_districts.json"
ACCOUNTS = {
    "北京市": "6a72f808ee584439c1498a48",
    "重庆市": "6a72f7195ef68d5087f51d7e",
    "成都市": "6aadf81ed033d36c0b7dfcd8",
    "深圳市": "6ad17a6dfb87f7a2682bfe9e",
}


def regions_of(city: str) -> list[str]:
    tree = json.loads(DISTRICTS.read_text(encoding="utf-8"))
    for prov, cities in tree.items():
        if city in cities:
            return cities[city]
        if prov == city:
            for regs in cities.values():
                return regs
    return []


pool = json.loads(POOL.read_text(encoding="utf-8"))
regions_map = pool.get("regions") or {}

for city, acc in ACCOUNTS.items():
    have = {k[len(city):] for k in regions_map if k.startswith(city)}
    missing = [r for r in regions_of(city) if r not in have]
    if not missing:
        continue
    print("\n" + "=" * 70)
    print("%s 缺 %d 个区县：%s" % (city, len(missing), "、".join(missing)))
    for r in missing:
        q = r
        try:
            p = subprocess.run([exe, "query", "locations", acc, "--query", q, "--json"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=45)
            d = json.loads(p.stdout or "{}")
            items = ((d.get("data") or {}).get("dataList") or [])
        except Exception as exc:
            print("  %-14s ERR %r" % (r, exc))
            continue
        cand, rejected = [], []
        for it in items:
            name = str(it.get("yixiaoerName") or "")
            raw = it.get("raw") or {}
            entry = {"name": name, "raw": raw}
            if not _address_matches(it, r):
                continue                      # 不是本区的
            if is_bad(name):
                rejected.append(name + "(黑名单)")
                continue
            if not is_target_poi(entry):
                cats = "|".join(poi_categories(entry))[:28]
                rejected.append("%s(%s)" % (name[:16], cats))
                continue
            cand.append(name)
        print("  %-14s 可用 %d 条 %s" % (r, len(cand), "、".join(cand[:3]) if cand else ""))
        if rejected:
            print("      被拒: " + "；".join(rejected[:4]))
        time.sleep(0.4)
