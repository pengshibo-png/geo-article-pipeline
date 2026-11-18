"""导出当前品牌资料全文（改前备份用）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
con = sqlite3.connect(ROOT / "data" / "app.db")
row = con.execute("SELECT content FROM brand WHERE tenant_id='local'").fetchone()
con.close()
c = (row[0] if row else "") or ""
bak = ROOT / "data" / "brand.backup.json"
bak.write_text(json.dumps({"content": c}, ensure_ascii=False, indent=1), encoding="utf-8")
print("字数:", len(c))
print("已备份:", bak)
print("=" * 60)
print(c)
