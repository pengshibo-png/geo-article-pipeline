"""体检文章档案：title / dir / status，以及正文里是否含标题标签。"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer import title_context  # noqa: E402

con = sqlite3.connect(ROOT / "data" / "app.db")
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT id, dir, title, status FROM article ORDER BY id").fetchall()
con.close()

print(f"文章档案 {len(rows)} 条\n")
year = f"{title_context.now_local().year}年"
no_year = [r for r in rows if year not in (r["title"] or "")]
long = [r for r in rows if len(r["title"] or "") > title_context._TITLE_MAX]
print(f"缺年份 {len(no_year)} 条 | 超 {title_context._TITLE_MAX} 字 {len(long)} 条\n")

print(f"{'id':>3} {'状态':<10}{'title（len）':<46}dir")
for r in rows[:12]:
    t = r["title"] or ""
    print(f"{r['id']:>3} {r['status']:<10}[{len(t)}] {t[:40]:<42}{r['dir'][:34]}")

# 正文里是否有标题标签（h1 / 首行标题）
ART = ROOT / "data" / "local" / "articles"
probe = rows[:5]
print("\n=== 正文头部（是否有 h1 / 标题）===")
for r in probe:
    d = ART / r["dir"]
    for name in ("03_final.html", "01_draft.html"):
        p = d / name
        if p.exists():
            h = p.read_text(encoding="utf-8")
            h1 = re.findall(r"<h1[^>]*>(.{0,60})", h)
            print(f"  {r['dir'][:30]:<32} {name:<14} h1={h1[:1] or '无'}")
            break
