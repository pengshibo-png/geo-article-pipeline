"""端到端验证：POI 池 → allocate() → 发布用的 location 结构是否真实可用。

`allocate()` 返回的就是发布时提交给蚁小二的 `location` 字段：
    {"data": {"yixiaoerId":..., "yixiaoerName":..., "raw": {...}}, "isScp": False}
所以这脚本等于**在不真发的前提下，把发布链路里最关键的"定位"跑了一遍**。
用临时 state，不落盘、不污染真实去重记录。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_location import allocate  # noqa: E402


def main() -> int:
    pool = json.loads(
        (ROOT / "data" / "local" / "douyin" / "poi_pool.json").read_text(
            encoding="utf-8"))
    regions = pool.get("regions") or {}
    print("POI 池：%d 个区县键 / %d 条 POI\n"
          % (len(regions), sum(len(v) for v in regions.values())))

    pairs = [("pack-demo-%02d" % i, "acc-demo-%02d" % i) for i in range(1, 13)]
    out = allocate(pairs, pool, {}, recent_n=5)

    provs = Counter()
    cities = Counter()
    print("%-12s %-24s %-22s %s" % ("派单", "POI 名称", "省/市/区", "经纬度"))
    print("-" * 100)
    ok = 0
    for (pid, aid), loc in out.items():
        if not loc:
            print("%-12s (未分配)" % aid)
            continue
        data = loc.get("data") or {}
        raw = data.get("raw") or {}
        ai = raw.get("address_info") or {}
        lat, lng = raw.get("poi_latitude"), raw.get("poi_longitude")
        provs[str(ai.get("province"))] += 1
        cities[str(ai.get("city"))] += 1
        ok += 1
        print("%-12s %-24s %-22s %s,%s" % (
            aid, str(data.get("yixiaoerName"))[:22],
            ("%s%s%s" % (ai.get("province") or "", ai.get("city") or "",
                         ai.get("district") or ""))[:20],
            lat, lng))

    print("-" * 100)
    print("分配成功 %d/%d" % (ok, len(pairs)))
    print("覆盖省份：%s" % dict(provs))
    print("覆盖城市：%s" % dict(cities))

    if ok:
        loc = next(v for v in out.values() if v)
        print("\n=== 抽样：一条完整 location（这就是提交给平台的字段）===")
        s = json.dumps(loc, ensure_ascii=False, indent=1)
        print(s[:1200])
        print("\n=== 关键字段核对 ===")
        d = loc["data"]
        ai = d["raw"]["address_info"]
        print("  poi_id（抖音 POI ID，发布时提交）: %s" % d.get("yixiaoerId"))
        print("  名称                            : %s" % d.get("yixiaoerName"))
        print("  省 / 市 / 区                    : %s / %s / %s"
              % (ai.get("province"), ai.get("city"), ai.get("district")))
        print("  经纬度                          : %s, %s"
              % (d["raw"].get("poi_latitude"), d["raw"].get("poi_longitude")))
        print("  抖音分类                        : %s"
              % (d["raw"].get("poi_frontend_type")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
