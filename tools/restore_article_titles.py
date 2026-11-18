"""从备份恢复文章标题原文（用于压缩策略调整后重跑）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
bak = ROOT / "data" / "article-titles.before-year.json"
rows = json.loads(bak.read_text(encoding="utf-8"))
con = sqlite3.connect(ROOT / "data" / "app.db")
for r in rows:
    con.execute("UPDATE article SET title=? WHERE id=? AND tenant_id='local'",
                (r["title"], r["id"]))
con.commit()
n = con.execute("SELECT COUNT(*) FROM article").fetchone()[0]
con.close()
print(f"已从备份恢复 {len(rows)} 条标题（库内共 {n} 条）")
