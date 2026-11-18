"""从备份恢复选题库标题原文（压缩策略调整后重跑用）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
bak = ROOT / "data" / "titles.before-year.json"
rows = json.loads(bak.read_text(encoding="utf-8"))
con = sqlite3.connect(ROOT / "data" / "app.db")
n = 0
for r in rows:
    con.execute("UPDATE title SET text=? WHERE id=? AND tenant_id='local'",
                (r["text"], r["id"]))
    n += 1
con.commit()
total = con.execute(
    "SELECT COUNT(*) FROM title WHERE tenant_id='local' AND deleted_at IS NULL"
).fetchone()[0]
con.close()
print(f"已从备份恢复 {n} 条选题标题（库内有效 {total} 条）")
print("下一步：python tools/fix_titles.py --apply")
