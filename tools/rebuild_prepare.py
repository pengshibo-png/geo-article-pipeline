"""准备重写：把「不合规且未发布」的文章退回选题池（2026-10-22）。

判定"不合规"：
- 缺规格七类要素里任一可判定项（Q&A/表格/定义句/编号列表/结论加粗/适用边界），或
- 可见字数 < 2000，或
- **标题本身需清洗**（正文没有 <h1>，标题只存在 article.title 里，
  只查正文会漏掉「2026年上海做非遗金缮划不划算？」这种标题级违规）

**已发布（published）不动正文**：线上内容以平台为准，重写本地只会让档案与线上
不一致（发布记录也会丢）。但它们若标题违规，仍会修标题。

处置：
1. 目录整体备份到 `data/_rebuild_backup/`（移动，可恢复）；
2. 删 article 记录；
3. 标题（已清洗）退回选题池：`title.used=0`；匹配不到就新建一条。
4. 保留但标题违规的：只更新 `article.title`，正文不动。

用法：.venv\\Scripts\\python.exe tools/rebuild_prepare.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer import compliance, title_context  # noqa: E402

ART = ROOT / "data" / "local" / "articles"
BAK = ROOT / "data" / "_rebuild_backup"
MIN_CHARS = 2000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, dir, title, status FROM article ORDER BY id").fetchall()

    todo, keep = [], []
    for r in rows:
        d = ART / r["dir"]
        p = d / "03_final.html"
        if not p.exists():
            p = d / "01_draft.html"
        if not p.exists():
            continue
        html = p.read_text(encoding="utf-8")
        miss = compliance.missing_elements(html)
        chars = compliance.visible_chars(html)
        raw_title = r["title"] or ""
        clean_title, _th = compliance.sanitize(raw_title, contact_mode="revise")
        clean_title = title_context.normalize_title(clean_title)
        title_dirty = clean_title != raw_title
        if title_dirty:
            miss = list(miss) + ["标题需清洗"]
        need = bool(miss) or chars < MIN_CHARS
        if r["status"] == "published":
            keep.append((r, "已发布·正文不重写", chars, miss, clean_title, title_dirty))
            continue
        if need:
            todo.append((r, chars, miss, clean_title, title_dirty))
        else:
            keep.append((r, "已合规", chars, miss, clean_title, title_dirty))

    print(f"待重写 {len(todo)} 篇；保留 {len(keep)} 篇\n")
    for r, chars, miss, clean_title, td in todo:
        print(f"  #{r['id']:<3} [{chars:>4}字] {'/'.join(miss)}  {r['title'][:24]}")
        if td:
            print(f"        标题清洗 → {clean_title}")
    print()
    for r, why, chars, miss, clean_title, td in keep:
        note = f"（标题将清洗 → {clean_title}）" if td else ""
        print(f"  [保留] #{r['id']:<3} {why} {note}")

    if not a.apply:
        print("\n（预览模式；加 --apply 生效）")
        con.close()
        return 0

    BAK.mkdir(parents=True, exist_ok=True)
    log = {"moved": [], "title_fixed": []}
    for r, _c, _m, clean_title, _td in todo:
        src = ART / r["dir"]
        if src.exists():
            dst = BAK / r["dir"]
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))
        con.execute("DELETE FROM article WHERE id=? AND tenant_id='local'", (r["id"],))
        hit = con.execute(
            "SELECT id FROM title WHERE tenant_id='local' AND text=? AND deleted_at IS NULL",
            (clean_title,)).fetchone()
        if hit:
            con.execute("UPDATE title SET used=0 WHERE id=?", (hit["id"],))
            how = "退回已有选题"
        else:
            con.execute(
                "INSERT INTO title (tenant_id, text, source, used) VALUES ('local', ?, 'ai', 0)",
                (clean_title,))
            how = "新建选题"
        log["moved"].append({"id": r["id"], "dir": r["dir"], "title": clean_title,
                             "title_action": how})
    # 保留但标题违规的：只改标题
    for r, _why, _c, _m, clean_title, td in keep:
        if td:
            con.execute("UPDATE article SET title=? WHERE id=? AND tenant_id='local'",
                        (clean_title, r["id"]))
            log["title_fixed"].append({"id": r["id"], "title": clean_title})
    con.commit()
    con.close()

    out = ROOT / "data" / "rebuild-prepare.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已处置 {len(todo)} 篇 → 备份 {BAK}")
    print(f"另修正标题 {len(log['title_fixed'])} 条；清单：{out}")
    print(f"下一步：触发批量生成 count={len(todo)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
