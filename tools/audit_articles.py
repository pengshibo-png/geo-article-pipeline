"""全量体检已生成文章：平台合规 + 规格要素 + 字数（只读，不改任何东西）。

用法：.venv\\Scripts\\python.exe tools\\audit_articles.py [--all]
默认只列**不合规**的；--all 列全部。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer import compliance, title_context  # noqa: E402

ART = ROOT / "data" / "local" / "articles"
MIN_CHARS = 2000

# 旧品牌资料特有的违禁表述（现在的品牌库已清掉，但老文章里还在）
LEGACY_BAD = ("非遗", "老字号", "全国连锁", "双直营", "区县独家", "一口价",
              "零遗失风险", "永久不坏", "百分百无痕", "无损修复", "彻底解决",
              "极低返修率", "高认可度", "无忧", "包治")


def read_body(d: Path) -> tuple[str, str]:
    for name in ("03_final.html", "01_draft.html"):
        p = d / name
        if p.exists():
            return name, p.read_text(encoding="utf-8")
    return "", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, dir, title, status FROM article ORDER BY id").fetchall()
    con.close()

    bad, ok = [], []
    for r in rows:
        d = ART / r["dir"]
        fname, html = read_body(d)
        if not html:
            bad.append((r, "文件缺失", ["无正文文件"], [], 0))
            continue
        # 用 sanitize 的检测能力（不写回）：hits 就是"本可被清掉的违规项"
        _clean, hits = compliance.sanitize(html, contact_mode="revise")
        text = compliance._TAG.sub("", html)
        legacy = [w for w in LEGACY_BAD if w in text]
        miss = compliance.missing_elements(html)
        chars = compliance.visible_chars(html)
        problems = []
        if hits:
            # 只保留"确定性违规"（号码/邮箱/外链/绝对化/导流），空壳/标点不算
            real = [h for h in hits if not h.startswith(("空", "悬空", "时效"))]
            if real:
                problems.append("违规:" + "、".join(sorted({h.split(":")[0] for h in real})))
        if legacy:
            problems.append("旧资料:" + "、".join(legacy))
        if chars < MIN_CHARS:
            problems.append(f"字数{chars}")
        if miss:
            problems.append("缺" + "/".join(miss))
        entry = (r, fname, problems, miss, chars)
        (bad if problems else ok).append(entry)

    print(f"文章 {len(rows)} 篇：有问题 {len(bad)} 篇，合规 {len(ok)} 篇\n")
    print(f"{'id':>3} {'状态':<10}{'字数':>5}  title / 问题")
    for r, fname, problems, miss, chars in bad:
        t = title_context.shortify(r["title"] or "", 24)
        print(f"{r['id']:>3} {r['status']:<10}{chars:>5}  {t}")
        print(f"      问题: {'；'.join(problems)}")
    if a.all and ok:
        print("\n=== 合规的 ===")
        for r, fname, _p, _m, chars in ok:
            print(f"{r['id']:>3} {r['status']:<10}{chars:>5}  "
                  f"{title_context.shortify(r['title'] or '', 30)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
