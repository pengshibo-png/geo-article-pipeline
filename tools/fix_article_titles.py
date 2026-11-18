"""批量校正「文章档案」里的标题：补当前年份 + 压到长度上限内（2026-10-22）。

**只改 `article.title`，不动 `article.dir`、不动磁盘目录名。**
原因：`dir` 是存储路径的唯一标识，改它要同步重命名目录、还要修正所有引用
（断点续跑、发布记录）；而档案页显示的是 `title`，目录名用户看不到。
正文里也没有 `<h1>`（已核对 43 篇全部无 h1），标题只存在这一个字段。

用法：.venv\\Scripts\\python.exe tools\\fix_article_titles.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer import title_context  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, dir, title, status FROM article ORDER BY id").fetchall()

    plan, unchanged = [], 0
    for r in rows:
        old = r["title"] or ""
        new = title_context.normalize_title(old)
        if new == old:
            unchanged += 1
        else:
            plan.append((r["id"], old, new, r["status"]))

    print(f"文章档案 {len(rows)} 条：需改 {len(plan)} 条，不变 {unchanged} 条\n")
    for tid, old, new, st in plan:
        print(f"#{tid:<3} [{len(old):>2}] {old}")
        print(f"     → [{len(new):>2}] {new}   ({st})")

    if not plan:
        con.close()
        return 0
    if not a.apply:
        print("\n（预览模式；加 --apply 生效）")
        con.close()
        return 0

    bak = ROOT / "data" / "article-titles.before-year.json"
    bak.write_text(json.dumps(
        [{"id": t, "title": o, "dir": next(x["dir"] for x in rows if x["id"] == t)}
         for t, o, _, _ in plan], ensure_ascii=False, indent=1), encoding="utf-8")

    for tid, _old, new, _st in plan:
        # 只动 title；dir 保持原样（见文件头说明）
        con.execute("UPDATE article SET title=? WHERE id=? AND tenant_id='local'",
                    (new, tid))
    con.commit()
    con.close()
    print(f"\n已更新 {len(plan)} 条标题（目录名未动）；备份：{bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
