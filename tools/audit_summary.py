"""文章体检汇总：按严重度分类，给处置建议（只读）。"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer import compliance  # noqa: E402

ART = ROOT / "data" / "local" / "articles"
MIN_CHARS = 2000
JUNK_CHARS = 1000          # 低于此判废稿
LEGACY_BAD = ("非遗", "老字号", "全国连锁", "双直营", "区县独家", "一口价",
              "零遗失风险", "永久不坏", "百分百无痕", "无损修复", "彻底解决",
              "极低返修率", "高认可度")


def main() -> int:
    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, dir, title, status FROM article ORDER BY id").fetchall()
    con.close()

    buckets: dict[str, list] = {"废稿": [], "已发布": [], "严重违规": [], "轻微": []}
    for r in rows:
        d = ART / r["dir"]
        html = ""
        for name in ("03_final.html", "01_draft.html"):
            p = d / name
            if p.exists():
                html = p.read_text(encoding="utf-8")
                break
        chars = compliance.visible_chars(html) if html else 0
        _c, hits = compliance.sanitize(html, contact_mode="revise")
        real = [h for h in hits if not h.startswith(("空", "悬空", "时效"))]
        text = compliance._TAG.sub("", html)
        legacy = [w for w in LEGACY_BAD if w in text]
        miss = compliance.missing_elements(html) if html else ["无正文"]

        if r["status"] == "published":
            buckets["已发布"].append((r, chars, real, legacy, miss))
        elif chars < JUNK_CHARS or not html:
            buckets["废稿"].append((r, chars, real, legacy, miss))
        elif real or legacy:
            buckets["严重违规"].append((r, chars, real, legacy, miss))
        elif miss or chars < MIN_CHARS:
            buckets["轻微"].append((r, chars, real, legacy, miss))

    print(f"总计 {len(rows)} 篇\n")
    for k in ("废稿", "严重违规", "轻微", "已发布"):
        v = buckets[k]
        print(f"=== {k}：{len(v)} 篇 ===")
        for r, chars, real, legacy, miss in v:
            tags = []
            if real:
                tags.append("违规:" + "、".join(sorted({h.split(':')[0] for h in real})))
            if legacy:
                tags.append("旧资料:" + "、".join(legacy[:4]))
            if miss:
                tags.append("缺" + "/".join(miss))
            if chars < MIN_CHARS:
                tags.append(f"字数{chars}")
            print(f"  #{r['id']:<3} [{chars:>4}字] {'；'.join(tags) or '仅字数略少'}")
        print()

    print("严重度分布：")
    c = Counter()
    for r, _ch, real, legacy, _m in rows and [x for v in buckets.values() for x in v]:
        for h in real:
            c[h.split(":")[0]] += 1
        for w in legacy:
            c["旧资料"] += 1
    for k, n in c.most_common():
        print(f"  {k}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
