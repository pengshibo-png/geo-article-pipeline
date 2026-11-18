"""把 expanded_districts.md 转成机器可读结构，供 POI 抓取脚本消费。

输出 data/local/expanded_districts.json：
{
  "北京市": {"北京市": ["东城区", ...]},
  "河南省": {"郑州市": ["中原区", ...], "开封市": [...]},
  ...
}

直辖市（北京/重庆）用「省=市」的双层占位，保持结构统一。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MD = ROOT / "expanded_districts.md"
OUT = ROOT / "data" / "local" / "expanded_districts.json"
FLAT = {"北京市", "重庆市", "成都市", "西安市", "深圳市"}   # 单列城市：无下级市，# 小节只是分类

tree: dict[str, dict[str, list[str]]] = {}
prov, city = None, None

for raw in MD.read_text(encoding="utf-8").splitlines():
    line = raw.rstrip()
    if line.startswith("## "):
        m = re.match(r"## [一二三四五六七八九十]+、(.+?)（", line)
        prov = m.group(1) if m else None
        city = None
        if prov:
            tree.setdefault(prov, {})
        continue
    if line.startswith("### "):
        if not prov:
            continue
        if prov in FLAT:
            city = prov                      # 直辖市：所有小节归到同名"市"
            tree[prov].setdefault(city, [])
            continue
        m = re.match(r"### (.+?)（", line)
        if m:
            city = m.group(1).strip()
            tree[prov].setdefault(city, [])
        continue
    if line.startswith((">", "|", "-", "`", "*", "#")) or not line.strip():
        continue
    if not (prov and city):
        continue
    for p in re.split(r"[、，]", line):
        p = p.strip().strip("（）()")
        if re.search(r"(区|县|市|林区|新区)$", p) and 1 < len(p) <= 20:
            if p not in tree[prov][city]:
                tree[prov][city].append(p)

# 统计
total = 0
print("%-16s%-8s%-8s%s" % ("省级单位", "市级", "区县", "样例"))
print("-" * 60)
for p, cities in tree.items():
    n = sum(len(v) for v in cities.values())
    total += n
    sample = next(iter(next(iter(cities.values())) or [""]), "")
    print("%-16s%-8s%-8s%s" % (p[:14], len(cities), n, sample))
print("-" * 60)
print("合计: %d 个区县，%d 个省级单位" % (total, len(tree)))

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(tree, ensure_ascii=False, indent=1), encoding="utf-8")
print("已写出:", OUT)
