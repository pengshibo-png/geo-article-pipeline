"""按省批量抓 POI：每个地级单位用它自己代理下的账号抓，逐市写盘。

背景（2026-11-02 实测）：
- `yxer query locations` 只能搜到**账号所在地**附近的 POI，跨市返回的是本地商家；
- 所以每个地级市必须有至少 1 个出口落在该市的账号；
- **一个市可以配多个号**，多号交叉能显著提高覆盖率（湖北黄冈 40% → 100% 靠 3 个号）；
- 快代理地区编码：直辖市取省级 code，普通城市取 `query proxy-areas` 里 cities 的 code。

用法：
    python tools/build_poi_by_province.py --province 湖南省              # 预览
    python tools/build_poi_by_province.py --province 湖南省 --apply      # 真抓
    python tools/build_poi_by_province.py --province 湖南省 --apply --cities 长沙市,株洲市
    python tools/build_poi_by_province.py --province 湖南省 --apply --only-missing

POI 过滤：`build_pool` → `_to_poi` 已强制过 `is_target_poi`（只收商场 / 中高端小区），
学校/小店铺/纯地址名另由 `is_bad()` 排除。
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

POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"
DISTRICTS = ROOT / "data" / "local" / "expanded_districts.json"

# ── 账号映射：市 → [账号 id, ...]（多个号会依次抓并去重合并）──────────────
# 数据来源：tools/probe_accounts_city.py 实测出口城市
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
    "深圳市": ["6ae4185f9dc04c194bfd2bb8"],
    "珠海市": ["6ae3163e381ee67120da7dda"],
    "汕头市": ["6adb276ce353fba14f3a7e81"],
    "佛山市": ["6ad17a6dfb87f7a2682bfe9e"],
    "江门市": ["6ad1798f9c29488112aa0ac2"],
    "湛江市": ["6ad179b19662552a52b9fdb0"],
    "茂名市": ["6ad1789f736ba199556afab7"],
    "肇庆市": ["6ad8992ae01687755065c7fb"],
    "惠州市": ["6ac6ee61fd4f1a74e5b9c89d", "6a72f808ee584439c1498a48"],
    "梅州市": ["6aadf81ed033d36c0b7dfcd8"],
    "汕尾市": ["6a72f808ee584439c1498a48"],
    "河源市": ["6a72f6f7e2bf8d8e7cb3e3bd"],
    "韶关市": ["6ad179cb736ba199556b16f2"],
    # 2026-11-02 晚补齐（用户配了新代理）
    "阳江市": ["6ae4757120b64c0b5ffd9507"],
    "清远市": ["6ae4185f9dc04c194bfd2bb8"],
    "潮州市": ["6ad17a6dfb87f7a2682bfe9e"],
    "揭阳市": ["6ad17a6dfb87f7a2682bfe9e", "6ad1789f736ba199556afab7"],
    "云浮市": ["6a72f808ee584439c1498a48"],
    "东莞市": ["6ae3163e381ee67120da7dda"],      # 不设区，按市级整体取
    "中山市": ["6ad8992ae01687755065c7fb"],      # 不设区，按市级整体取
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
    "自治区直辖县级市": ["6ae4185f9dc04c194bfd2bb8"],   # 克拉玛依/胡杨河号
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

PROVINCE_ACCOUNTS = {"湖北省": HUBEI, "湖南省": HUNAN, "江西省": JIANGXI,
                     "广东省": GUANGDONG, "福建省": FUJIAN,
                     "新疆维吾尔自治区": XINJIANG, "云南省": YUNNAN}

# 这些「市」本身是县级单位，池里的键直接用区县名
# （东莞市、中山市是「不设区的地级市」，没有下级区县，按市级整体取）
FLAT = {"省直辖县级市", "自治区直辖县级市", "神农架林区", "东莞市", "中山市"}


def _as_list(v) -> list[str]:
    return [v] if isinstance(v, str) else list(v or [])


def load_province(province: str) -> dict:
    tree = json.loads(DISTRICTS.read_text(encoding="utf-8"))
    return tree.get(province) or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--province", required=True)
    ap.add_argument("--cities", default="", help="只跑这几个市（逗号分隔），默认全部")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--queries", default="{r},{r}商场,{r}小区,{r}广场")
    ap.add_argument("--only-missing", action="store_true",
                    help="只抓池里还没有 POI 的区县（补覆盖率用）")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    cities_map = load_province(a.province)
    if not cities_map:
        print("清单里没有这个省：", a.province)
        return 2
    acc_map = PROVINCE_ACCOUNTS.get(a.province) or {}
    if not acc_map:
        print("没有为这个省配置账号映射：", a.province)
        return 2

    only = {x.strip() for x in a.cities.split(",") if x.strip()}
    plan = []
    for c, regs in cities_map.items():
        if not only or c in only:
            if c in acc_map:
                # 不设区的地级市（东莞/中山）清单里区县为空 → 按市级整体取
                use = list(regs) or [c]
                plan.append((c, _as_list(acc_map[c]), use))
    skipped = [c for c in cities_map if c not in acc_map]

    total_regions = sum(len(r) for _, _, r in plan)
    print("%s：%d 个地级单位，本次 %d 个 / %d 个区县"
          % (a.province, len(cities_map), len(plan), total_regions))
    if skipped:
        print("  无号跳过：%s" % "、".join(skipped))
    for c, accs, regs in plan:
        print("  %-22s %2d 个区县  -> %d 个账号" % (c, len(regs), len(accs)))
    if not a.apply:
        print("\n（预览模式；加 --apply 实际抓取）")
        return 0

    from app.services.douyin_poi_build import build_pool

    qs = [x.strip() for x in a.queries.split(",") if x.strip()]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = POOL.with_suffix(".json.bak-%s" % stamp)
    shutil.copy2(POOL, bak)
    print("\n备份: %s\n关键词: %s\n" % (bak.name, qs))

    pool = json.loads(POOL.read_text(encoding="utf-8"))
    regions_map = pool.setdefault("regions", {})

    if a.only_missing:
        trimmed = []
        for c, accs, regs in plan:
            miss = [r for r in regs
                    if not (regions_map.get(
                        r if c in FLAT else "%s%s" % (c, r)) or [])]
            if miss:
                trimmed.append((c, accs, miss))
        before = sum(len(r) for _, _, r in plan)
        plan = trimmed
        total_regions = sum(len(r) for _, _, r in plan)
        print("  只补缺失：%d 个区县待抓（%d 个已覆盖）"
              % (total_regions, before - total_regions))
        if not plan:
            print("  没有缺失的区县")
            return 0

    grand_new = grand_done = grand_target = 0
    report = []

    for idx, (city, accs, regs) in enumerate(plan, 1):
        t0 = time.time()
        print("[%d/%d] %s（%d 区县 × %d 号）..."
              % (idx, len(plan), city, len(regs), len(accs)), end="", flush=True)
        merged: dict[str, list] = {}
        seen: set[str] = set()
        for acc in accs:
            try:
                built = build_pool(regs, yxer_account_id=acc, max_pages=a.pages,
                                   queries=qs)
            except Exception as exc:  # noqa: BLE001 - 单号失败不中断
                print(" [%s 失败 %s]" % (acc[-4:], type(exc).__name__), end="")
                continue
            for r, pois in (built.get("regions") or {}).items():
                for p in pois:
                    pid = str(p.get("id"))
                    if pid in seen:
                        continue
                    seen.add(pid)
                    merged.setdefault(r, []).append(p)

        added = done = 0
        for r in regs:
            pois = merged.get(r) or []
            if not pois:
                continue
            key = r if city in FLAT else "%s%s" % (city, r)
            old = {str(p.get("id")) for p in (regions_map.get(key) or [])}
            fresh = [p for p in pois if str(p.get("id")) not in old]
            if fresh:
                regions_map.setdefault(key, []).extend(fresh)
                added += len(fresh)
            done += 1
        POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
        grand_new += added
        grand_done += done
        grand_target += len(regs)
        report.append((city, done, len(regs), added, "%.0fs" % (time.time() - t0)))
        print(" 覆盖 %d/%d  新增 %d  (%.0fs)"
              % (done, len(regs), added, time.time() - t0))

    pool.setdefault("meta", {})["extended_at"] = datetime.now().strftime(
        "%Y-%m-%d %H:%M")
    pool["meta"]["extended_province"] = a.province
    POOL.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")

    print("\n=== %s 汇总 ===" % a.province)
    for city, done, target, added, secs in report:
        print("  %-24s %3d/%-3d  %4d  %s" % (city, done, target, added, secs))
    print("\n  合计覆盖 %d/%d（%.1f%%），新增 %d 条"
          % (grand_done, grand_target,
             100.0 * grand_done / max(1, grand_target), grand_new))
    print("  池内总 POI: %d" % sum(len(v) for v in regions_map.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
