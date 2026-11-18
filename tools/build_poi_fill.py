"""补漏：只抓「池里还没有 POI 的区县」，用更细的商场关键词。

用法：
    python tools/build_poi_fill.py --cities 北京市,重庆市,成都市,深圳市 [--apply]

与 `build_poi_city.py` 的区别：
- 只处理**缺失**的区县，不重复抓已有的；
- 关键词更偏向商场命名（购物中心/百货/商业/生活广场/广场），用于补上一轮漏掉的。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
DISTRICTS = ROOT / "data" / "local" / "expanded_districts.json"
FLAT = {"北京市", "重庆市", "成都市", "西安市", "深圳市"}
EXTRA = {
    "天津市": ["和平区", "河东区", "河西区", "南开区", "河北区", "红桥区", "东丽区",
            "西青区", "津南区", "北辰区", "武清区", "宝坻区", "滨海新区", "宁河区",
            "静海区", "蓟州区"],
}
FILL_QUERIES = ["{r}购物中心", "{r}百货", "{r}商业", "{r}生活广场", "{r}广场"]
ACCOUNTS = {
    "北京市": "6a72f808ee584439c1498a48",
    "重庆市": "6a72f7195ef68d5087f51d7e",
    "成都市": "6aadf81ed033d36c0b7dfcd8",
    "西安市": "6adb276ce353fba14f3a7e81",
    "深圳市": "6ad17a6dfb87f7a2682bfe9e",
    "天津市": "6ae3163e381ee67120da7dda",
}


def regions_of(city: str) -> list[str]:
    tree = json.loads(DISTRICTS.read_text(encoding="utf-8")) if DISTRICTS.exists() else {}
    for prov, cities in tree.items():
        if city in cities:
            return cities[city]
        if prov == city:
            for regs in cities.values():
                return regs
    return EXTRA.get(city, [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default="")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    cities = ([x.strip() for x in a.cities.split(",") if x.strip()]
              if a.cities else list(ACCOUNTS))

    pool = json.loads(POOL.read_text(encoding="utf-8"))
    regions_map = pool.setdefault("regions", {})

    plan = {}
    for city in cities:
        all_regions = regions_of(city)
        have = {k[len(city):] for k in regions_map if k.startswith(city)}
        missing = [r for r in all_regions if r not in have]
        if missing:
            plan[city] = missing
        print("%-8s 共 %2d 区，已有 %2d，缺 %2d：%s"
              % (city, len(all_regions), len(all_regions) - len(missing),
                 len(missing), "、".join(missing[:10])))

    total = sum(len(v) for v in plan.values())
    print("\n合计缺 %d 个区县" % total)
    if not total:
        print("没有需要补的")
        return 0
    if not a.apply:
        print("（预览模式；加 --apply 实际抓取）")
        return 0

    from app.services.douyin_poi_build import build_pool

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = POOL.with_suffix(".json.bak-fill-%s" % stamp)
    shutil.copy2(POOL, bak)
    print("\n备份:", bak)

    filled_total = 0
    for city, missing in plan.items():
        acc = ACCOUNTS.get(city)
        if not acc:
            print("跳过（无账号）：", city)
            continue
        print("\n--- 补 %s（%d 个区）---" % (city, len(missing)))
        built = build_pool(missing, yxer_account_id=acc, max_pages=a.pages,
                           queries=FILL_QUERIES)
        got = built.get("regions") or {}
        filled = 0
        for r in missing:
            pois = got.get(r) or []
            if pois:
                regions_map[city + r] = pois
                filled += 1
                filled_total += len(pois)
        print("  补上 %d/%d 个区，新增 %d 条"
              % (filled, len(missing), sum(len(got.get(r) or []) for r in missing)))

    pool.setdefault("meta", {})["filled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    print("\n补漏完成：新增 %d 条；池内 %d 键 / %d POI"
          % (filled_total, len(regions_map),
             sum(len(v) for v in regions_map.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
