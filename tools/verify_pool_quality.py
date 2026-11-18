"""验证 POI 池质量：关键商场是否保留、垃圾是否清掉。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"

d = json.loads(POOL.read_text(encoding="utf-8"))
regions = d.get("regions") or {}
names = {str(p.get("name")) for v in regions.values() for p in v}

print("池内: %d 键 / %d POI\n" % (len(regions), sum(len(v) for v in regions.values())))
print("=== 应保留（真商场）===")
for probe in ["豫园商城", "绿地缤纷城", "BFC", "来福士", "新天地", "万象城",
              "大悦城", "万达", "太古里", "合生汇", "环球港", "爱琴海"]:
    hit = [n for n in names if probe in n]
    print("  %-12s %s" % (probe, ("✓ %d 条: %s" % (len(hit), "、".join(hit[:2])))
                          if hit else "✗ 未找到"))

print("\n=== 应清除（垃圾）===")
for probe in ["菜市场", "宿舍", "政府", "税务局", "医院", "物流", "公司",
              "公园", "食堂", "创业园区"]:
    hit = [n for n in names if probe in n]
    print("  %-12s %s" % (probe, ("✗ 仍残留 %d 条: %s" % (len(hit), "、".join(hit[:2])))
                          if hit else "✓ 已清空"))

print("\n=== 天津（本轮新抓）===")
tj = {k: v for k, v in regions.items() if k.startswith("天津市")}
print("  %d 个区 / %d 条" % (len(tj), sum(len(v) for v in tj.values())))
for k in sorted(tj):
    print("    %-14s %d  %s" % (k, len(tj[k]),
                                "、".join(str(p.get("name"))[:12] for p in tj[k][:2])))
