"""复查选题库标题：是否都带当前年份、是否都在长度上限内。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer import title_context  # noqa: E402
from app.timeutil import now_local  # noqa: E402

LIMIT = title_context._TITLE_MAX
con = sqlite3.connect(ROOT / "data" / "app.db")
rows = con.execute(
    "SELECT id, text, used FROM title WHERE tenant_id='local' AND deleted_at IS NULL "
    "ORDER BY id").fetchall()
con.close()

year = f"{now_local().year}年"
no_year = [(i, t) for i, t, _ in rows if year not in (t or "")]
too_long = [(i, t, len(t)) for i, t, _ in rows if len(t or "") > LIMIT]
print(f"有效标题 {len(rows)} 条")
print(f"缺年份 {len(no_year)} 条 | 超 {LIMIT} 字 {len(too_long)} 条")
for i, t in no_year[:6]:
    print(f"  缺年份 #{i} {t}")
for i, t, n in too_long[:6]:
    print(f"  超长 {n} 字 #{i} {t}")
if rows:
    print("\n样例：")
    for _i, t, _u in rows[:5]:
        print(f"  [{len(t)}] {t}")
