"""批量处置历史文章：废稿移走 + 违规确定性清洗（不重写、不花 LLM 成本）。

处置规则：
1. **废稿**（可见字数 < 1000，含早期截断产物如 7 字/11 字）→ 整个目录移到
   `data/_deleted_articles/`（移动而非删除，可恢复），并删 article 记录；
2. **严重违规**（号码/导流/绝对化/旧资料违禁词）→ 用 `compliance.sanitize`
   确定性清洗 03_final.html 与 01_draft.html，原文件备份到 `data/_pre_repair/`；
3. **已发布（published）** → **不动档案**：线上内容以平台为准，改档案只会让本地
   与线上不一致。只报告，由人去平台处理；
4. **轻微**（仅缺要素，字数够）→ 不动。

用法：.venv\\Scripts\\python.exe tools\\repair_articles.py [--apply]
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
DEAD = ROOT / "data" / "_deleted_articles"
BAK = ROOT / "data" / "_pre_repair"
JUNK_CHARS = 1000
LEGACY_BAD = ("非遗", "老字号", "全国连锁", "双直营", "区县独家", "一口价",
              "零遗失风险", "永久不坏", "百分百无痕", "无损修复", "彻底解决",
              "极低返修率", "高认可度")


def body_of(d: Path) -> str:
    p = d / "03_final.html"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, dir, title, status FROM article ORDER BY id").fetchall()

    junk, dirty, skipped = [], [], []
    for r in rows:
        d = ART / r["dir"]
        html = body_of(d)
        if not html:
            junk.append(r)
            continue
        chars = compliance.visible_chars(html)
        _c, hits = compliance.sanitize(html, contact_mode="revise")
        real = [h for h in hits if not h.startswith(("空", "悬空", "时效"))]
        text = compliance._TAG.sub("", html)
        legacy = [w for w in LEGACY_BAD if w in text]
        if r["status"] == "published":
            skipped.append((r, real, legacy))
        elif chars < JUNK_CHARS:
            junk.append(r)
        elif real or legacy:
            dirty.append((r, real, legacy))

    print(f"废稿待移走 {len(junk)} 篇；违规待清洗 {len(dirty)} 篇；"
          f"已发布跳过 {len(skipped)} 篇\n")
    for r in junk:
        print(f"  [废稿] #{r['id']:<3} {compliance.visible_chars(body_of(ART / r['dir'])):>4} 字  "
              f"{r['title'][:30]}")
    for r, real, legacy in dirty:
        print(f"  [清洗] #{r['id']:<3} {'、'.join(sorted({h.split(':')[0] for h in real}) + legacy)[:60]}")
    if skipped:
        print("\n  已发布（档案不动，需去平台自行处理）：")
        for r, real, legacy in skipped:
            tag = "、".join(sorted({h.split(':')[0] for h in real}) + legacy) or "仅缺要素"
            print(f"    #{r['id']} {r['title'][:28]}  → {tag}")

    if not a.apply:
        print("\n（预览模式；加 --apply 生效）")
        con.close()
        return 0

    DEAD.mkdir(parents=True, exist_ok=True)
    BAK.mkdir(parents=True, exist_ok=True)
    log = {"junk": [], "cleaned": []}

    for r in junk:
        src = ART / r["dir"]
        if src.exists():
            dst = DEAD / r["dir"]
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))
        con.execute("DELETE FROM article WHERE id=? AND tenant_id='local'", (r["id"],))
        log["junk"].append({"id": r["id"], "dir": r["dir"], "title": r["title"]})
    con.commit()

    for r, _real, _legacy in dirty:
        d = ART / r["dir"]
        row = {"id": r["id"], "dir": r["dir"], "files": {}}
        for name in ("03_final.html", "01_draft.html"):
            p = d / name
            if not p.exists():
                continue
            html = p.read_text(encoding="utf-8")
            (BAK / f"{r['id']}-{name}").write_text(html, encoding="utf-8")
            new, hits = compliance.sanitize(html, contact_mode="revise")
            if new != html:
                p.write_text(new, encoding="utf-8")
                row["files"][name] = len(hits)
        log["cleaned"].append(row)

    out = ROOT / "data" / "repair-log.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    con.close()
    print(f"\n已移走废稿 {len(junk)} 篇 → {DEAD}")
    print(f"已清洗 {len(dirty)} 篇 → 原文件备份 {BAK}")
    print(f"处置日志：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
