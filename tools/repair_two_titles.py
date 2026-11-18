"""修复被过度压缩的两条标题（上限从 30 调到 33 之后）。

#19 原「本地服务预约前：服务范围、额外收费、质保承诺一次问全」(27)
   被压成 11 字，副标题信息全丢 → 恢复为完整版 + 年份（31 字）
#36 原问句末尾的「吗？」被截掉，读起来像陈述句 → 补回问号（31 字）
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer import title_context  # noqa: E402

FIXES = {
    19: "2026年本地服务预约前：服务范围、额外收费、质保承诺一次问全",
    36: "2026年上海聚贤阁老人留下的玉镯无痕修复先检后修流程规范吗？",
}

con = sqlite3.connect(ROOT / "data" / "app.db")
con.row_factory = sqlite3.Row
bak = []
for tid, new in FIXES.items():
    row = con.execute("SELECT text FROM title WHERE id=? AND tenant_id='local'",
                      (tid,)).fetchone()
    if not row:
        print(f"  #{tid} 不存在，跳过")
        continue
    bak.append({"id": tid, "text": row["text"]})
    con.execute("UPDATE title SET text=? WHERE id=? AND tenant_id='local'", (new, tid))
    print(f"  #{tid} {row['text']}  →  {new}  ({len(new)} 字)")
con.commit()
con.close()
out = ROOT / "data" / "titles.repair-backup.json"
out.write_text(json.dumps(bak, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n备份：{out}")
print("当前上限:", title_context._TITLE_MAX)
