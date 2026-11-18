"""补覆盖率：对指定城市，用**多个能搜到该城市的账号** + **更多关键词**只补缺失区县。

为什么需要：一个账号搜一轮通用词只返回 12 条（按距离），覆盖不到全部区县；
   多换几个账号、多换几个关键词（含本地商超品牌），能显著补齐。

用法：
    python tools/build_poi_boost.py --cities 郑州市,鹤壁市,开封市 [--apply]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_filter import is_bad, is_target_poi  # noqa: E402
from app.services.publish.yxer_cli import run_yxer  # noqa: E402
from app.services.publish.yxer_imgtext import _extract_location_list  # noqa: E402

POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
DISTRICTS = ROOT / "data" / "local" / "expanded_districts.json"
CITY_ACC = ROOT / "data" / "city-accounts.json"
ACC_CITY = ROOT / "data" / "accounts-city.json"

KEYWORDS = [
    "生活广场", "购物中心", "商场", "百货", "广场", "超市", "商城",
    # 全国连锁商超/百货
    "万达", "大润发", "家乐福", "沃尔玛", "华润万家", "世纪联华", "银泰",
    # 河南本地常见
    "丹尼斯", "大商", "新玛特", "大张", "胖东来", "大商新玛特", "正道",
    "购物广场", "商业街", "步行街", "生活超市", "购物",
]


def regions_of(city: str) -> list[str]:
    tree = json.loads(DISTRICTS.read_text(encoding="utf-8"))
    for prov, cities in tree.items():
        if city in cities:
            return cities[city]
        if prov == city:
            for regs in cities.values():
                return regs
    return []


def accounts_for(city: str) -> list[str]:
    """能搜到该城市的账号：来自探测结果 + 人工映射。"""
    ids = []
    if ACC_CITY.exists():
        for r in json.loads(ACC_CITY.read_text(encoding="utf-8")):
            if city in str(r.get("city") or ""):
                ids.append(r["id"])
    if CITY_ACC.exists():
        mp = json.loads(CITY_ACC.read_text(encoding="utf-8"))
        if mp.get(city):
            ids.append(mp[city])
    return list(dict.fromkeys(ids))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--pages", type=int, default=1, help="每个关键词翻几页")
    a = ap.parse_args()
    cities = [x.strip() for x in a.cities.split(",") if x.strip()]

    pool = json.loads(POOL.read_text(encoding="utf-8"))
    regions_map = pool.setdefault("regions", {})

    plan = {}
    for city in cities:
        have = {k[len(city):] for k in regions_map if k.startswith(city)}
        missing = [r for r in regions_of(city) if r not in have]
        accs = accounts_for(city)
        print("%-8s 缺 %2d 个，可用账号 %d 个" % (city, len(missing), len(accs)))
        if missing and accs:
            plan[city] = (missing, accs)

    if not plan:
        print("\n没有需要补的")
        return 0
    if not a.apply:
        print("\n（预览模式；加 --apply 实际抓取）")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = POOL.with_suffix(".json.bak-boost-%s" % stamp)
    shutil.copy2(POOL, bak)
    print("\n备份:", bak)

    total_added = 0
    for city, (missing, accs) in plan.items():
        want = set(missing)
        found: dict[str, list] = {}
        seen: set[str] = set()
        t0 = time.time()
        for acc in accs:
            for q in KEYWORDS:
                page = None
                for _ in range(max(1, a.pages)):
                    cmd = ["query", "locations", acc, "--query", q, "--json"]
                    if page is not None:
                        cmd += ["--next-page", str(page)]
                    try:
                        raw = run_yxer(cmd, timeout=60)
                    except Exception:
                        break
                    for it in _extract_location_list(raw) or []:
                        ai = ((it.get("raw") or {}).get("address_info")) or {}
                        if str(ai.get("city") or "") != city:
                            continue
                        dist = str(ai.get("district") or "").strip()
                        if dist not in want:
                            continue
                        name = str(it.get("yixiaoerName") or "")
                        if not name or is_bad(name):
                            continue
                        entry = {"id": str(it.get("yixiaoerId") or ""), "name": name,
                                 "raw": it.get("raw") if isinstance(it.get("raw"), dict) else {},
                                 "desc": str(it.get("yixiaoerDesc") or "")}
                        if not is_target_poi(entry):
                            continue
                        if entry["id"] in seen:
                            continue
                        seen.add(entry["id"])
                        found.setdefault(dist, []).append(entry)
                    page = (raw.get("data") or {}).get("nextPage")
                    if not page:
                        break
        for dist, pois in found.items():
            k = city + dist
            regions_map.setdefault(k, [])
            exist = {str(p.get("id")) for p in regions_map[k]}
            for p in pois:
                if str(p.get("id")) not in exist:
                    regions_map[k].append(p)
                    total_added += 1
        print("  %-8s 补齐 %d/%d 区，新增 %d 条（%.0f 秒）"
              % (city, len(found), len(missing),
                 sum(len(v) for v in found.values()), time.time() - t0))
        if found:
            print("      " + "、".join("%s(%d)" % (d, len(v)) for d, v in found.items()))

    pool.setdefault("meta", {})["boosted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    print("\n补覆盖完成：新增 %d 条；池内 %d 键 / %d POI"
          % (total_added, len(regions_map),
             sum(len(v) for v in regions_map.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
