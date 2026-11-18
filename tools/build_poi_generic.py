"""用「商场通用词」搜索并按 POI 自带的 district 分配区县（补覆盖率）。

为什么需要它：`--query <区县名>` 是名称模糊匹配，搜「海淀区」只返回名字里带
「海淀区」的 POI（全是学校/政府），真商场名字里没有区名 → 永远抓不到。
改用「生活广场/购物中心/百货/商场/超市/广场」搜，再从 `raw.address_info.district`
读区县，落到对应键上。实测北京 16 区全覆盖。

用法：
    python tools/build_poi_generic.py --cities 北京市,重庆市,成都市,深圳市 [--apply]
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

from app.services.douyin_poi_filter import is_bad, is_target_poi, poi_categories  # noqa: E402
from app.services.publish.yxer_cli import run_yxer  # noqa: E402

POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
DISTRICTS = ROOT / "data" / "local" / "expanded_districts.json"
ACCOUNTS = {
    "北京市": "6a72f808ee584439c1498a48",
    "重庆市": "6a72f7195ef68d5087f51d7e",
    "成都市": "6aadf81ed033d36c0b7dfcd8",
    "西安市": "6adb276ce353fba14f3a7e81",
    "深圳市": "6ad17a6dfb87f7a2682bfe9e",
    "天津市": "6ae3163e381ee67120da7dda",
}
GENERIC_QUERIES = ["生活广场", "购物中心", "商场", "百货", "广场", "超市", "商城"]


def regions_of(city: str) -> list[str]:
    tree = json.loads(DISTRICTS.read_text(encoding="utf-8"))
    for prov, cities in tree.items():
        if city in cities:
            return cities[city]
        if prov == city:
            for regs in cities.values():
                return regs
    return []


def to_poi(item: dict) -> dict | None:
    """把搜索结果转成池条目（不再按搜索词校验地址，改为按 district 分配）。"""
    if not isinstance(item, dict):
        return None
    pid = str(item.get("yixiaoerId") or "").strip()
    name = str(item.get("yixiaoerName") or "").strip()
    if not pid or not name or is_bad(name):
        return None
    entry = {"id": pid, "name": name,
             "raw": item.get("raw") if isinstance(item.get("raw"), dict) else {},
             "desc": str(item.get("yixiaoerDesc") or "")}
    if not is_target_poi(entry):
        return None
    return entry


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default="")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--map", default="data/city-accounts.json",
                    help="城市→账号 映射文件（JSON），用于新省份")
    a = ap.parse_args()
    accounts = dict(ACCOUNTS)
    mp = ROOT / a.map
    if mp.exists():
        accounts.update(json.loads(mp.read_text(encoding="utf-8")))
    cities = ([x.strip() for x in a.cities.split(",") if x.strip()]
              if a.cities else list(accounts))

    pool = json.loads(POOL.read_text(encoding="utf-8"))
    regions_map = pool.setdefault("regions", {})

    # 清理脏键（如「光明区（功能区：大鹏新区」）
    dirty = [k for k in list(regions_map) if "（" in k or "(" in k]
    for k in dirty:
        regions_map.pop(k, None)
    if dirty:
        print("清理脏键 %d 个：%s" % (len(dirty), "、".join(dirty[:3])))

    plan = {}
    for city in cities:
        have = {k[len(city):] for k in regions_map if k.startswith(city)}
        missing = [r for r in regions_of(city) if r not in have]
        if missing:
            plan[city] = missing
        print("%-8s 缺 %d 个：%s" % (city, len(missing), "、".join(missing) or "无"))

    if not plan:
        print("\n所有城市都已覆盖")
        return 0
    if not a.apply:
        print("\n（预览模式；加 --apply 实际抓取）")
        return 0

    from app.services.publish.yxer_imgtext import _extract_location_list

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = POOL.with_suffix(".json.bak-generic-%s" % stamp)
    shutil.copy2(POOL, bak)
    print("\n备份:", bak)

    added_total = 0
    for city, missing in plan.items():
        acc = accounts.get(city)
        if not acc:
            continue
        want = set(missing)
        allregs = set(regions_of(city))
        found: dict[str, list] = {}
        seen: set[str] = set()
        print("\n--- %s：用通用词搜索（%d 个词）---" % (city, len(GENERIC_QUERIES)))
        for q in GENERIC_QUERIES:
            try:
                raw = run_yxer(["query", "locations", acc, "--query", q, "--json"],
                               timeout=60)
            except Exception:
                continue
            for it in _extract_location_list(raw) or []:
                ai = ((it.get("raw") or {}).get("address_info")) or {}
                if str(ai.get("city") or "") != city:
                    continue
                dist = str(ai.get("district") or "").strip()
                if dist not in want:
                    continue
                poi = to_poi(it)
                if poi and poi["id"] not in seen:
                    seen.add(poi["id"])
                    found.setdefault(dist, []).append(poi)
        for dist, pois in found.items():
            k = city + dist
            regions_map.setdefault(k, [])
            existing = {str(p.get("id")) for p in regions_map[k]}
            for p in pois:
                if str(p.get("id")) not in existing:
                    regions_map[k].append(p)
                    added_total += 1
        print("  补上 %d/%d 个区，新增 %d 条"
              % (len(found), len(missing), sum(len(v) for v in found.values())))
        if found:
            print("  " + "、".join("%s(%d)" % (d, len(v)) for d, v in found.items()))

    pool.setdefault("meta", {})["generic_filled_at"] = datetime.now().strftime(
        "%Y-%m-%d %H:%M")
    POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    print("\n补漏完成：新增 %d 条；池内 %d 键 / %d POI"
          % (added_total, len(regions_map),
             sum(len(v) for v in regions_map.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
