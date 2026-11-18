"""按「单个城市」抓 POI 并合并进池（2026-11-01）。

前提：给该城市绑定的账号**代理已实际生效**（proxyIp 非 null，实测出口城市正确）。

用法：
    python tools/build_poi_city.py --city 天津市 --account <yxer_id>            # 预览
    python tools/build_poi_city.py --city 天津市 --account <yxer_id> --apply    # 真抓

POI 过滤：build_pool → _to_poi 已强制过 `is_target()`（只收商场 / 中高端小区），
学校类另由 `is_bad()` 排除。
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

# 清单外的补充城市（用户 2026-11-01 提到要加）
EXTRA_CITIES = {
    "天津市": ["和平区", "河东区", "河西区", "南开区", "河北区", "红桥区",
            "东丽区", "西青区", "津南区", "北辰区", "武清区", "宝坻区",
            "滨海新区", "宁河区", "静海区", "蓟州区"],
}


def regions_of(city: str) -> tuple[str, list[str]]:
    """返回 (省级名, 区县列表)。优先取 expanded_districts.json。"""
    f = ROOT / "data" / "local" / "expanded_districts.json"
    tree = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    for prov, cities in tree.items():
        if city in cities:
            return prov, cities[city]
        if prov == city:                       # 直辖市/单列城市
            for regs in cities.values():
                return prov, regs
    if city in EXTRA_CITIES:
        return city, EXTRA_CITIES[city]
    return "", []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--queries", default="{r},{r}商场,{r}小区,{r}广场",
                    help="逗号分隔的关键词模板，{r} 会被替换成区县名")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    prov, regions = regions_of(a.city)
    if not regions:
        print("找不到这个城市的区县清单：", a.city)
        print("清单内可选省级：", "、".join(json.loads(
            (ROOT / "data" / "local" / "expanded_districts.json").read_text(
                encoding="utf-8")).keys()))
        return 2

    print("%s（%s）：%d 个区县" % (a.city, prov, len(regions)))
    if not a.apply:
        print("  " + "、".join(regions[:20]) + ("…" if len(regions) > 20 else ""))
        print("\n（预览模式；加 --apply 实际抓取）")
        return 0

    from app.services.douyin_poi_build import build_pool

    print("\n开始抓取（每区县最多 %d 页）…" % a.pages)
    qs = [x.strip() for x in a.queries.split(",") if x.strip()]
    print("关键词模板:", qs)
    built = build_pool(regions, yxer_account_id=a.account, max_pages=a.pages,
                       queries=qs)
    got = built.get("regions") or {}
    failed = built.get("failed") or []

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = POOL.with_suffix(".json.bak-%s" % stamp)
    shutil.copy2(POOL, bak)
    pool = json.loads(POOL.read_text(encoding="utf-8"))
    regions_map = pool.setdefault("regions", {})
    merged = 0
    for r in regions:
        pois = got.get(r) or []
        if pois:
            regions_map["%s%s" % (a.city, r)] = pois     # 键：城市+区县，不混淆
            merged += 1
    pool.setdefault("meta", {})["extended_at"] = datetime.now().strftime(
        "%Y-%m-%d %H:%M")
    POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")

    total = sum(len(v) for v in regions_map.values())
    print("\n=== %s 结果 ===" % a.city)
    print("  目标区县    : %d" % len(regions))
    print("  有 POI 区县 : %d（覆盖率 %.1f%%）"
          % (merged, 100.0 * merged / max(1, len(regions))))
    print("  新增 POI    : %d" % sum(len(got.get(r) or []) for r in regions))
    print("  池内总 POI  : %d" % total)
    print("  接口失败    : %d %s" % (len(failed), failed[:8]))
    empty = [r for r in regions if not (got.get(r) or [])]
    print("  0 POI 区县  : %d %s" % (len(empty), empty[:8]))
    print("  备份        : %s" % bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
