"""校验 expanded_districts.md：严格按「## 省 → ### 市 → 条目行」解析并计数。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MD = ROOT / "expanded_districts.md"

prov = None
items: list[str] = []
results: list[tuple[str, int, int]] = []
ann = None


def flush() -> None:
    if prov and items:
        uniq = list(dict.fromkeys(items))
        results.append((prov, ann or 0, len(uniq)))


for raw in MD.read_text(encoding="utf-8").splitlines():
    line = raw.rstrip()
    if line.startswith("## "):
        flush()
        prov, items, ann = None, [], None
        m = re.match(r"## [一二三四五六七八九十]+、(.+?)（", line)
        if m:
            prov = m.group(1)
            a = re.search(r"(\d+)\s*个县级区划", line)
            ann = int(a.group(1)) if a else None
        continue
    if line.startswith("### ") or not line.strip():
        continue                      # 小节标题不参与计数
    if line.startswith((">", "|", "-", "`", "*", "#")):
        continue
    if line.strip() == "---":
        continue
    for p in re.split(r"[、，]", line):
        p = p.strip().strip("（）()")
        if re.search(r"(区|县|市|林区|新区)$", p) and 1 < len(p) <= 20:
            items.append(p)
flush()

print("%-18s%-8s%-8s%s" % ("省级单位", "标注", "实解析", "差异"))
print("-" * 50)
tot = 0
bad = 0
for name, a, n in results:
    tot += n
    diff = "OK" if a == n else ("(无标注)" if not a else str(n - a))
    if a and a != n:
        bad += 1
    print("%-18s%-8s%-8s%s" % (name[:16], a or "-", n, diff))
print("-" * 50)
print("实解析合计: %d；标注不符: %d 项" % (tot, bad))
