"""按省统计 POI 覆盖率（逐市 + 缺失区县清单）。

用法：python tools/report_province.py 湖北省
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISTRICTS = ROOT / "data" / "local" / "expanded_districts.json"
POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"

# 这些「市」本身是县级单位，池里的键直接用区县名
FLAT = {"省直辖县级市", "自治区直辖县级市", "神农架林区"}


def main() -> int:
    prov = sys.argv[1] if len(sys.argv) > 1 else "湖北省"
    tree = json.loads(DISTRICTS.read_text(encoding="utf-8"))
    cities = tree.get(prov) or {}
    pool = json.loads(POOL.read_text(encoding="utf-8"))
    regions = pool.get("regions") or {}
    if not cities:
        print("清单里没有：", prov)
        return 2

    total = covered = pois = 0
    missing: list[str] = []
    print("%-24s %6s %6s %6s" % ("城市", "目标", "已覆盖", "POI"))
    print("-" * 52)
    for city, regs in cities.items():
        keys = []
        for x in regs:
            k = x if city in FLAT else city + x
            if regions.get(k):
                keys.append(k)
            else:
                missing.append("%s%s" % (city, x))
        n = sum(len(regions[k]) for k in keys)
        total += len(regs)
        covered += len(keys)
        pois += n
        flag = "" if len(keys) == len(regs) else "  缺 %d" % (len(regs) - len(keys))
        print("%-24s %6d %6d %6d%s" % (city[:22], len(regs), len(keys), n, flag))

    print("-" * 52)
    print("%-24s %6d %6d %6d" % ("合计", total, covered, pois))
    print("覆盖率 %.1f%%" % (100.0 * covered / max(1, total)))
    if missing:
        print("\n缺 %d 个区县：" % len(missing))
        for i in range(0, len(missing), 6):
            print("  " + "、".join(missing[i:i + 6]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
