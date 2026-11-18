"""挑出本轮重写里**仍不达标**的篇目，备份+退回选题池，准备再跑一轮。

判定不达标（与验收脚本同口径）：
- 缺规格七类要素任一可判定项
- 或可见字数 < 2000
- 或正文仍有确定性违规（号码/外链/绝对化/导流）

已发布（published）不动。
用法：.venv\Scripts\python.exe tools\retry_failed.py [--apply]
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
from app.services.writer import compliance  # noqa: E402

ART = ROOT / "data" / "local" / "articles"
BAK = ROOT / "data" / "_rebuild_backup"
PLAN = ROOT / "data" / "rebuild-prepare.json"
MIN = 2000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, dir, title, status FROM article").fetchall()
    targets = set()
    if PLAN.exists():
        targets = {x["title"] for x in json.loads(PLAN.read_text(encoding="utf-8"))["moved"]}
    bad = []
    for r in rows:
        if r["title"] not in targets or r["status"] == "published":
            continue
        d = ART / r["dir"]
        p = d / "03_final.html"
        if not p.exists():
            p = d / "01_draft.html"
        if not p.exists():
            continue
        html = p.read_text(encoding="utf-8")
        miss = compliance.missing_elements(html)
        chars = compliance.visible_chars(html)
        _c, hits = compliance.sanitize(html, contact_mode="revise")
        real = [h for h in hits if not h.startswith(("空", "悬空", "时效"))]
        if miss or chars < MIN or real:
            bad.append((r, chars, miss, real))
    print("仍不达标 %d 篇：" % len(bad))
    for r, chars, miss, real in bad:
        print("  #%-3s %4d字 缺%s 违规%s  %s" % (
            r["id"], chars, "/".join(miss) or "-",
            ",".join(sorted({h.split(":")[0] for h in real})) or "-", r["title"][:22]))
    if not bad or not a.apply:
        if not a.apply:
            print("\n（预览模式；加 --apply 生效）")
        con.close()
        return 0
    BAK.mkdir(parents=True, exist_ok=True)
    out = []
    for r, _c, _m, _v in bad:
        src = ART / r["dir"]
        if src.exists():
            dst = BAK / r["dir"]
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))
        con.execute("DELETE FROM article WHERE id=? AND tenant_id='local'", (r["id"],))
        hit = con.execute(
            "SELECT id FROM title WHERE tenant_id='local' AND text=? AND deleted_at IS NULL",
            (r["title"],)).fetchone()
        if hit:
            con.execute("UPDATE title SET used=0 WHERE id=?", (hit["id"],))
        else:
            con.execute(
                "INSERT INTO title (tenant_id, text, source, used) VALUES ('local', ?, 'ai', 0)",
                (r["title"],))
        out.append({"id": r["id"], "title": r["title"]})
    con.commit()
    con.close()
    (ROOT / "data" / "retry-failed.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已退回 %d 篇 -> data/retry-failed.json" % len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())