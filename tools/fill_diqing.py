"""把迪庆州能抓到的 POI 尽量抓全（重点补香格里拉市）。

背景（2026-11-03 实测）：迪庆的号在香格里拉，距德钦 180km、维西 200km，
`query locations` 按距离返回，搜「德钦」「维西」拿到的是丽江的同名商家。
所以只能把**香格里拉市**抓全，德钦/维西在抖音 POI 库里没有可用商业点。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_filter import is_bad, is_target_poi  # noqa: E402
from app.services.publish.yxer_cli import run_yxer  # noqa: E402
from app.services.publish.yxer_imgtext import _extract_location_list  # noqa: E402

POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
CITY = "迪庆藏族自治州"
ACC = "6ad17a6dfb87f7a2682bfe9e"      # 修镯实录（丽江/迪庆/大理）
WANT = {"香格里拉市", "德钦县", "维西傈僳族自治县"}

QUERIES = [
    "商场", "超市", "百货", "购物中心", "生活广场", "广场", "步行街",
    "商业街", "农贸市场", "市场", "小区", "花园", "公寓", "商厦",
    "香格里拉商场", "香格里拉超市", "香格里拉小区", "香格里拉广场",
    "独克宗", "建塘", "德钦", "维西", "中甸",
]


def to_poi(item):
    pid = str(item.get("yixiaoerId") or "").strip()
    name = str(item.get("yixiaoerName") or "").strip()
    if not pid or not name or is_bad(name):
        return None
    e = {"id": pid, "name": name,
         "raw": item.get("raw") if isinstance(item.get("raw"), dict) else {},
         "desc": str(item.get("yixiaoerDesc") or "")}
    return e if is_target_poi(e) else None


def main() -> int:
    apply = "--apply" in sys.argv
    found: dict[str, list] = {}
    seen: set[str] = set()
    for q in QUERIES:
        try:
            raw = run_yxer(["query", "locations", ACC, "--query", q, "--json"],
                           timeout=60)
        except Exception:
            continue
        for it in _extract_location_list(raw) or []:
            ai = ((it.get("raw") or {}).get("address_info")) or {}
            if str(ai.get("city") or "") != CITY:
                continue
            d = str(ai.get("district") or "").strip()
            if d not in WANT:
                continue
            p = to_poi(it)
            if p and p["id"] not in seen:
                seen.add(p["id"])
                found.setdefault(d, []).append(p)

    print("抓到 %d 条：" % sum(len(v) for v in found.values()))
    for d, v in found.items():
        print("  %-14s %d 条: %s" % (d, len(v),
                                     "、".join(x["name"][:14] for x in v[:5])))
    if not apply:
        print("\n（预览模式；加 --apply 写入）")
        return 0
    pool = json.loads(POOL.read_text(encoding="utf-8"))
    regions = pool.setdefault("regions", {})
    added = 0
    for d, pois in found.items():
        key = CITY + d
        old = {str(p.get("id")) for p in (regions.get(key) or [])}
        fresh = [p for p in pois if str(p["id"]) not in old]
        if fresh:
            regions.setdefault(key, []).extend(fresh)
            added += len(fresh)
    POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    print("\n新增 %d 条；池内 %d 键 / %d POI"
          % (added, len(regions), sum(len(v) for v in regions.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
