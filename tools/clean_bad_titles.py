"""软删除标题库里含违规词的历史标题（2026-10-22）。

用软删（deleted_at）而不是物理删除 —— 与 Article/Title 的既有设计一致
（`deleted_at` 非空即视为已删，唯一索引也按它分区），可回滚。

用法：.venv\\Scripts\\python.exe tools\\clean_bad_titles.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAD = ("非遗", "老字号", "国家级", "独家", "一口价", "唯一", "顶级",
       "最好", "最佳", "百分百", "永久", "零风险", "保证", "根治", "彻底")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, text, used, deleted_at FROM title WHERE tenant_id='local'").fetchall()
    bad = [r for r in rows if any(w in (r["text"] or "") for w in BAD)
           and not r["deleted_at"]]
    print(f"标题库 {len(rows)} 条；待软删 {len(bad)} 条：")
    for r in bad:
        print(f"  #{r['id']} used={r['used']}  {r['text']}")
    if not bad:
        con.close()
        return 0
    if not a.apply:
        print("\n（预览模式；加 --apply 生效）")
        con.close()
        return 0

    bak = ROOT / "data" / "titles.before-clean.json"
    bak.write_text(json.dumps([dict(r) for r in bad], ensure_ascii=False, indent=1),
                   encoding="utf-8")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.executemany("UPDATE title SET deleted_at=? WHERE id=? AND tenant_id='local'",
                    [(now, r["id"]) for r in bad])
    con.commit()
    left = con.execute(
        "SELECT COUNT(*) FROM title WHERE tenant_id='local' AND deleted_at IS NULL").fetchone()[0]
    con.close()
    print(f"\n已软删 {len(bad)} 条；剩余有效标题 {left} 条")
    print(f"备份：{bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
