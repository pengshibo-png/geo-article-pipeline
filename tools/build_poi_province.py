"""按省份批量抓 POI 并合并进 poi_pool.json（2026-11-01）。

前提：**已给抓取账号绑定该省代理**（蚁小二网页后台操作，CLI 无设置命令）。
原因见 POI扩展方案.md：`yxer query locations` 只按账号出口 IP 的位置返回附近 POI，
`--latitude/--longitude/--keyword` 全部无效（已实测）。

用法：
    # 先看清单
    python tools/build_poi_province.py --province 北京市 --account <yxer_id> --dry-run
    # 真抓（抓完自动备份并合并）
    python tools/build_poi_province.py --province 北京市 --account <yxer_id> --apply

键格式：`省+市+区县`（如「河南郑州市金水区」）；直辖市/单列城市为 `省+区县`（如「北京朝阳区」）。
与现有 198 个裸区县名键**完全隔离**，互不覆盖 —— 同名区县不会混。
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

DISTRICTS = ROOT / "data" / "local" / "expanded_districts.json"
POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
FLAT = {"北京市", "重庆市", "成都市", "西安市", "深圳市"}


def region_key(prov: str, city: str, region: str) -> str:
    """拼池键：非单列城市带市名，避免跨市同名。"""
    if prov in FLAT or city == prov:
        return f"{prov}{region}"
    return f"{prov}{city}{region}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--province", required=True, help="省名，如 北京市 / 河南省")
    ap.add_argument("--account", required=True, help="用于抓取的 yxer 账号 id")
    ap.add_argument("--city", default=None, help="只抓该省下的某个市")
    ap.add_argument("--pages", type=int, default=3, help="每地区最多翻几页")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    tree = json.loads(DISTRICTS.read_text(encoding="utf-8"))
    if a.province not in tree:
        print("清单里没有这个省：", a.province)
        print("可选：", "、".join(tree))
        return 2
    cities = tree[a.province]
    if a.city:
        if a.city not in cities:
            print("该省没有这个市：", a.city, " 可选：", "、".join(cities))
            return 2
        cities = {a.city: cities[a.city]}

    pairs: list[tuple[str, str, str]] = []      # (city, region, key)
    for city, regions in cities.items():
        for r in regions:
            pairs.append((city, r, region_key(a.province, city, r)))

    print("%s：%d 个市 / %d 个区县" % (a.province, len(cities), len(pairs)))
    if not a.apply:
        for city, r, k in pairs[:30]:
            print("   %-10s %-14s -> %s" % (city, r, k))
        if len(pairs) > 30:
            print("   …（其余 %d 个）" % (len(pairs) - 30))
        print("\n（预览模式；加 --apply 实际抓取）")
        return 0

    from app.services.douyin_poi_build import build_pool

    print("\n开始抓取（每地区最多 %d 页），请确保账号已绑定「%s」代理…" % (a.pages, a.province))
    built = build_pool([p[1] for p in pairs], yxer_account_id=a.account,
                       max_pages=a.pages)
    got = built.get("regions") or {}
    failed = built.get("failed") or []

    # 备份 + 合并
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = POOL.with_suffix(".json.bak-%s" % stamp)
    shutil.copy2(POOL, bak)
    pool = json.loads(POOL.read_text(encoding="utf-8"))
    regions = pool.setdefault("regions", {})
    merged = 0
    for city, r, key in pairs:
        pois = got.get(r) or []
        if pois:
            regions[key] = pois
            merged += 1
    pool.setdefault("meta", {})["extended_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")

    total_pois = sum(len(v) for v in regions.values())
    print("\n=== 结果 ===")
    print("  目标区县      : %d" % len(pairs))
    print("  抓到 POI 的   : %d（覆盖率 %.1f%%）" % (merged, 100.0 * merged / max(1, len(pairs))))
    print("  POI 总条数    : %d（本次新增 %d）"
          % (total_pois, sum(len(got.get(r) or []) for _, r, _ in pairs)))
    print("  接口失败区县  : %d %s" % (len(failed), failed[:10]))
    empty = [r for _, r, _ in pairs if not (got.get(r) or [])]
    print("  0 POI 区县    : %d %s" % (len(empty), empty[:10]))
    print("  备份          : %s" % bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
