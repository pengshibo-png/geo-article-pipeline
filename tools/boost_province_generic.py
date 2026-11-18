"""用「通用商超词 + 多账号」补缺口：按 POI 自带的 district 精确分派。

为什么比「区县名 + 商场词」有效（2026-11-02 湖北实测）：
- 搜「红安县商场」这类组合词，返回的是**账号附近**的同名商家，远县一律 0 条；
- 但搜「中百仓储」「万达广场」这种**通用商超词**，返回的 POI 自带
  `raw.address_info.city/district`，按 district 分派就能一次覆盖多个远县。

用法：
    python tools/boost_province_generic.py --province 湖北省            # 预览
    python tools/boost_province_generic.py --province 湖北省 --apply
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

# 通用商超/商业词：跨区县命中率高（POI 自带 district，可精确分派）
GENERIC = [
    "中百仓储", "武商", "中商", "大润发", "沃尔玛", "永辉超市", "盒马",
    "万达广场", "吾悦广场", "购物中心", "生活广场", "百货", "商场", "广场",
    "超市", "小区",
    # 2026-11-02 实测补：县城里大量商业点叫「XX步行街」「XX商业街」，
    # 不含「商场/广场」字样；漏了这两个词，团风县这种县城会一直 0 条。
    "步行街", "商业街", "银泰", "国际商贸城",
    # 湖南本地商超（2026-11-02）：地市州县城里这些牌子的命中率明显更高
    "友阿", "通程", "步步高", "平和堂", "家润多", "友谊商城",
    # 江西本地商超
    "旺中旺", "洪客隆", "联盛", "天虹",
    # 广东本地商超/百货
    "广百", "天河城", "正佳", "永旺", "岁宝", "茂业", "海雅", "华润万家",
    "天虹商场", "万象城", "海岸城", "壹方城", "卓悦汇", "太阳新天地",
    # 福建本地商超
    "新华都", "夏商", "见福", "中闽百汇", "夏商百货",
    # 云南本地商超
    "昆百大", "嘉华", "一心堂", "紫气东来", "沃尔玛", "家乐福",
]

# 一个市可以有多个号（2026-11-02 实测出口城市）；多号交叉能显著提高覆盖
HUBEI = {
    "武汉市": ["6ae4757120b64c0b5ffd9507", "6ad17a6dfb87f7a2682bfe9e",
            "6ad1789f736ba199556afab7"],
    "黄石市": ["6ae4185f9dc04c194bfd2bb8", "6ad17a6dfb87f7a2682bfe9e",
            "6ad1789f736ba199556afab7"],
    "十堰市": ["6ae3163e381ee67120da7dda"],
    "宜昌市": ["6adb276ce353fba14f3a7e81", "6ad179cb736ba199556b16f2"],
    "襄阳市": ["6ad8992ae01687755065c7fb"],
    "鄂州市": ["6ae4185f9dc04c194bfd2bb8", "6ad17a6dfb87f7a2682bfe9e",
            "6ad1789f736ba199556afab7"],
    "荆门市": ["6ad179cb736ba199556b16f2"],
    "孝感市": ["6ad179b19662552a52b9fdb0", "6a72f7195ef68d5087f51d7e"],
    "荆州市": ["6ad1798f9c29488112aa0ac2", "6a72f7195ef68d5087f51d7e"],
    "黄冈市": ["6ae4185f9dc04c194bfd2bb8", "6ad17a6dfb87f7a2682bfe9e",
            "6ad1789f736ba199556afab7"],
    "咸宁市": ["6ac6ee61fd4f1a74e5b9c89d"],
    "随州市": ["6ac6edc98b2e3bd89d1fb536"],
    "恩施土家族苗族自治州": ["6aadf81ed033d36c0b7dfcd8"],
    "省直辖县级市": ["6a72f6f7e2bf8d8e7cb3e3bd", "6a72f7195ef68d5087f51d7e"],
}
HUNAN = {
    "长沙市": ["6ae4185f9dc04c194bfd2bb8", "6ad1798f9c29488112aa0ac2",
            "6ae3163e381ee67120da7dda", "6ae4757120b64c0b5ffd9507"],
    "株洲市": ["6ae4185f9dc04c194bfd2bb8"],
    "湘潭市": ["6a72f7195ef68d5087f51d7e", "6ae4185f9dc04c194bfd2bb8",
            "6ae3163e381ee67120da7dda"],
    "衡阳市": ["6adb276ce353fba14f3a7e81"],
    "邵阳市": ["6ad8992ae01687755065c7fb"],
    "岳阳市": ["6ad17a6dfb87f7a2682bfe9e", "6ad1798f9c29488112aa0ac2"],
    "常德市": ["6ad179cb736ba199556b16f2"],
    "张家界市": ["6ad179b19662552a52b9fdb0"],
    "益阳市": ["6ad1798f9c29488112aa0ac2"],
    "郴州市": ["6ad1789f736ba199556afab7"],
    "永州市": ["6ac6ee61fd4f1a74e5b9c89d"],
    "怀化市": ["6ad179b19662552a52b9fdb0", "6a72f808ee584439c1498a48"],
    "娄底市": ["6a72f7195ef68d5087f51d7e"],
    "湘西土家族苗族自治州": ["6ad179b19662552a52b9fdb0", "6a72f6f7e2bf8d8e7cb3e3bd"],
}

JIANGXI = {
    "南昌市": ["6aadf81ed033d36c0b7dfcd8", "6ae4757120b64c0b5ffd9507"],
    "景德镇市": ["6ae4185f9dc04c194bfd2bb8"],
    "萍乡市": ["6ad1789f736ba199556afab7", "6ae3163e381ee67120da7dda"],
    "九江市": ["6ad8992ae01687755065c7fb"],
    "新余市": ["6ad17a6dfb87f7a2682bfe9e"],
    "鹰潭市": ["6ad179cb736ba199556b16f2"],
    "赣州市": ["6ad179b19662552a52b9fdb0"],
    "吉安市": ["6ad17a6dfb87f7a2682bfe9e", "6ad1798f9c29488112aa0ac2",
            "6ae3163e381ee67120da7dda"],
    "宜春市": ["6ad17a6dfb87f7a2682bfe9e", "6ad1789f736ba199556afab7"],
    "抚州市": ["6ad179cb736ba199556b16f2", "6aadf81ed033d36c0b7dfcd8"],
    "上饶市": ["6a72f7195ef68d5087f51d7e", "6ae4185f9dc04c194bfd2bb8",
            "6ad179cb736ba199556b16f2"],
}

GUANGDONG = {
    "广州市": ["6ad17a6dfb87f7a2682bfe9e", "6ae4757120b64c0b5ffd9507"],
    "深圳市": [],                  # 号已切到清远，深圳暂无可用号
    "珠海市": ["6ae3163e381ee67120da7dda"],
    "汕头市": ["6adb276ce353fba14f3a7e81", "6ad17a6dfb87f7a2682bfe9e"],
    "佛山市": ["6ad17a6dfb87f7a2682bfe9e"],
    "江门市": ["6ad1798f9c29488112aa0ac2"],
    "湛江市": ["6ad179b19662552a52b9fdb0"],
    "茂名市": ["6ad1789f736ba199556afab7"],
    "肇庆市": ["6ad8992ae01687755065c7fb", "6a72f808ee584439c1498a48"],
    "惠州市": ["6ac6ee61fd4f1a74e5b9c89d"],
    "梅州市": ["6aadf81ed033d36c0b7dfcd8"],
    "汕尾市": [],                  # 号已切到肇庆/云浮
    "河源市": ["6a72f6f7e2bf8d8e7cb3e3bd"],
    "韶关市": ["6ad179cb736ba199556b16f2"],
    "阳江市": ["6ae4757120b64c0b5ffd9507"],
    "清远市": ["6ae4185f9dc04c194bfd2bb8"],
    "潮州市": ["6ad17a6dfb87f7a2682bfe9e"],
    "揭阳市": ["6ad17a6dfb87f7a2682bfe9e", "6ad1789f736ba199556afab7"],
    "云浮市": ["6a72f808ee584439c1498a48"],
    "东莞市": ["6ae3163e381ee67120da7dda"],
    "中山市": ["6ad8992ae01687755065c7fb"],
}

FUJIAN = {
    "福州市": ["6a72f6f7e2bf8d8e7cb3e3bd", "6ae3163e381ee67120da7dda",
            "6ae4757120b64c0b5ffd9507"],
    "厦门市": ["6ae4185f9dc04c194bfd2bb8", "6ad1798f9c29488112aa0ac2"],
    "莆田市": ["6ae3163e381ee67120da7dda"],
    "三明市": ["6ad8992ae01687755065c7fb"],
    "泉州市": ["6ad179b19662552a52b9fdb0", "6ae3163e381ee67120da7dda"],
    "漳州市": ["6ad1798f9c29488112aa0ac2"],
    "南平市": ["6ac6edc98b2e3bd89d1fb536"],
    "龙岩市": ["6a72f7195ef68d5087f51d7e"],
    "宁德市": ["6a72f6f7e2bf8d8e7cb3e3bd"],
}

XINJIANG = {
    "乌鲁木齐市": ["6ad8992ae01687755065c7fb", "6ae4757120b64c0b5ffd9507"],
    "克拉玛依市": ["6ae4185f9dc04c194bfd2bb8"],
    "吐鲁番市": ["6ae3163e381ee67120da7dda"],
    "哈密市": ["6adb276ce353fba14f3a7e81"],
    "昌吉回族自治州": ["6ad8992ae01687755065c7fb"],
    "博尔塔拉蒙古自治州": ["6ad17a6dfb87f7a2682bfe9e"],
    "巴音郭楞蒙古自治州": ["6ad179cb736ba199556b16f2"],
    "阿克苏地区": ["6ad179b19662552a52b9fdb0"],
    "克孜勒苏柯尔克孜自治州": ["6ad1798f9c29488112aa0ac2",
                        "6ad1789f736ba199556afab7"],
    "喀什地区": ["6ad1798f9c29488112aa0ac2", "6ad1789f736ba199556afab7"],
    "和田地区": ["6ac6ee61fd4f1a74e5b9c89d"],
    "伊犁哈萨克自治州": ["6ac6edc98b2e3bd89d1fb536"],
    "塔城地区": ["6a72f808ee584439c1498a48"],
    "阿勒泰地区": ["6a72f7195ef68d5087f51d7e"],
    "自治区直辖县级市": [
        "6ad8992ae01687755065c7fb", "6ae4757120b64c0b5ffd9507",   # 乌鲁木齐/昌吉
        "6ae4185f9dc04c194bfd2bb8",                              # 克拉玛依/胡杨河
        "6ad179b19662552a52b9fdb0",                              # 阿克苏/阿拉尔
        "6ad1798f9c29488112aa0ac2", "6ad1789f736ba199556afab7",  # 喀什/图木舒克
        "6a72f7195ef68d5087f51d7e",                              # 阿勒泰/北屯
        "6ad179cb736ba199556b16f2",                              # 巴音郭楞/铁门关
        "6ad17a6dfb87f7a2682bfe9e",                              # 博尔塔拉/双河
        "6ac6edc98b2e3bd89d1fb536",                              # 伊犁/可克达拉
        "6ac6ee61fd4f1a74e5b9c89d",                              # 和田/昆玉
        "6adb276ce353fba14f3a7e81",                              # 哈密/新星
        "6a72f808ee584439c1498a48",                              # 塔城/白杨
    ],
}

YUNNAN = {
    "昆明市": ["6ae3163e381ee67120da7dda", "6ae4757120b64c0b5ffd9507"],
    "曲靖市": ["6ae4185f9dc04c194bfd2bb8"],
    "玉溪市": ["6ae3163e381ee67120da7dda"],
    "保山市": ["6adb276ce353fba14f3a7e81"],
    "昭通市": ["6ad8992ae01687755065c7fb"],
    "丽江市": ["6ad17a6dfb87f7a2682bfe9e"],
    "普洱市": ["6ad179cb736ba199556b16f2"],
    "临沧市": ["6ad179b19662552a52b9fdb0"],
    "楚雄彝族自治州": ["6ad1798f9c29488112aa0ac2"],
    "红河哈尼族彝族自治州": ["6ad1789f736ba199556afab7"],
    "文山壮族苗族自治州": ["6ac6ee61fd4f1a74e5b9c89d"],
    "西双版纳傣族自治州": ["6ac6edc98b2e3bd89d1fb536"],
    "大理白族自治州": ["6ad17a6dfb87f7a2682bfe9e", "6a72f808ee584439c1498a48"],
    "德宏傣族景颇族自治州": ["6a72f7195ef68d5087f51d7e"],
    "怒江傈僳族自治州": ["6a72f6f7e2bf8d8e7cb3e3bd"],
    "迪庆藏族自治州": ["6ad17a6dfb87f7a2682bfe9e"],
}

PROVINCES = {"湖北省": HUBEI, "湖南省": HUNAN, "江西省": JIANGXI,
             "广东省": GUANGDONG, "福建省": FUJIAN,
             "新疆维吾尔自治区": XINJIANG, "云南省": YUNNAN}
FLAT = {"省直辖县级市", "自治区直辖县级市", "神农架林区"}


def to_poi(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    pid = str(item.get("yixiaoerId") or "").strip()
    name = str(item.get("yixiaoerName") or "").strip()
    if not pid or not name or is_bad(name):
        return None
    entry = {"id": pid, "name": name,
             "raw": item.get("raw") if isinstance(item.get("raw"), dict) else {},
             "desc": str(item.get("yixiaoerDesc") or "")}
    return entry if is_target_poi(entry) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--province", required=True)
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    cities = (json.loads(DISTRICTS.read_text(encoding="utf-8")).get(a.province) or {})
    acc_map = PROVINCES.get(a.province) or {}
    if not cities or not acc_map:
        print("没有该省的清单或账号映射：", a.province)
        return 2

    pool = json.loads(POOL.read_text(encoding="utf-8"))
    regions = pool.setdefault("regions", {})

    # 找出每个市还缺哪些区县
    plan: dict[str, list[str]] = {}
    for city, regs in cities.items():
        miss = [r for r in regs
                if not (regions.get(r if city in FLAT else city + r) or [])]
        if miss and city in acc_map:
            plan[city] = miss

    total_miss = sum(len(v) for v in plan.values())
    print("%s 还缺 %d 个区县，分布在 %d 个市" % (a.province, total_miss, len(plan)))
    for c, m in plan.items():
        print("  %-22s 缺 %d：%s" % (c, len(m), "、".join(m)))
    if not plan:
        print("没有缺口")
        return 0
    if not a.apply:
        print("\n（预览模式；加 --apply 实际抓取）")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = POOL.with_suffix(".json.bak-boost-%s" % stamp)
    shutil.copy2(POOL, bak)
    print("\n备份:", bak.name)

    grand = 0
    for city, miss in plan.items():
        want = set(miss)
        accs = acc_map[city]
        found: dict[str, list] = {}
        seen: set[str] = set()
        t0 = time.time()
        for acc in accs:
            # ① 先按「区县简称」搜：搜「团风」比「团风县商场」有效得多
            #    （后者是名称模糊匹配，县城里根本没有叫「团风县商场」的地方）
            for d0 in miss:
                dshort = d0
                for suf in ("县", "区", "市"):
                    dshort = dshort.replace(suf, "")
                if not dshort or dshort == d0:
                    continue
                try:
                    raw = run_yxer(["query", "locations", acc, "--query",
                                    dshort, "--json"], timeout=60)
                except Exception:
                    continue
                for it in _extract_location_list(raw) or []:
                    ai = ((it.get("raw") or {}).get("address_info")) or {}
                    # 省直辖县级市（仙桃/潜江/天门）的 POI 里 city 就是「仙桃市」，
                    # 不是分组名「省直辖县级市」—— 这类跳过城市校验，只按 district 匹配
                    if city not in FLAT and str(ai.get("city") or "") != city:
                        continue
                    d = str(ai.get("district") or "").strip()
                    if d not in want:
                        continue
                    poi = to_poi(it)
                    if poi and poi["id"] not in seen:
                        seen.add(poi["id"])
                        found.setdefault(d, []).append(poi)

            # ② 再按通用商超词搜
            for q in GENERIC:
                try:
                    raw = run_yxer(["query", "locations", acc, "--query", q,
                                    "--json"], timeout=60)
                except Exception:
                    continue
                for it in _extract_location_list(raw) or []:
                    ai = ((it.get("raw") or {}).get("address_info")) or {}
                    # 省直辖县级市（仙桃/潜江/天门）的 POI 里 city 就是「仙桃市」，
                    # 不是分组名「省直辖县级市」—— 这类跳过城市校验，只按 district 匹配
                    if city not in FLAT and str(ai.get("city") or "") != city:
                        continue
                    d = str(ai.get("district") or "").strip()
                    if d not in want:
                        continue
                    poi = to_poi(it)
                    if poi and poi["id"] not in seen:
                        seen.add(poi["id"])
                        found.setdefault(d, []).append(poi)
        added = 0
        for d, pois in found.items():
            key = d if city in FLAT else city + d
            regions.setdefault(key, []).extend(pois)
            added += len(pois)
        grand += added
        POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
        print("  %-22s 补齐 %d/%d 区，新增 %d 条 (%.0fs)%s"
              % (city, len(found), len(miss), added, time.time() - t0,
                 ("  → " + "、".join(found)) if found else ""))

    print("\n补漏完成：新增 %d 条；池内 %d 键 / %d POI"
          % (grand, len(regions), sum(len(v) for v in regions.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
