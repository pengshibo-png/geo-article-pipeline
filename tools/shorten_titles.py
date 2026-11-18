"""把文章 / 选题库里超过 30 字的标题压到 30 字以内（2026-11-03 用户要求）。

为什么：超过 30 字在很多抖音号上会被平台 schema 拒，整批发不出去。

压缩策略（按优先级）：
1. **标点压缩** —— 按问号 / 冒号 / 分句边界截，语义完整（首选）
2. **去品牌名** —— 标题里的「上海聚贤阁」对读者没价值、还占 6 个字，
                    去掉后常常刚好放得下，且比硬截断完整
3. 硬截断 —— 兜底

用法：
    python tools/shorten_titles.py            # 预览
    python tools/shorten_titles.py --apply    # 写库（先自动备份 db）
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer import compliance, title_context  # noqa: E402

DB = ROOT / "data" / "app.db"
LIMIT = title_context._TITLE_MAX

# 标题里的品牌名对读者没价值（正文里会出现），但占 3–6 个字。
# 压到 30 时优先去掉它 —— 否则「2026年上海聚贤阁多段碎裂的玉镯无痕修复和别家
# 修法差在哪」会被硬截成「…差在哪无」这种半句。
BRANDS = ("上海聚贤阁", "聚贤阁")


def smart_shorten(text: str) -> tuple[str, str]:
    """返回 (新标题, 用了哪种策略)。

    **先去品牌名再判断**：`normalize_title` 内部一定会截到 30，所以"先压再看"
    永远走不到第二步。正确顺序是先看去掉品牌名之后原文本身够不够短 ——
    够短就完全不用截断，语义最完整。
    """
    # ⓪ 先清违规词（如「第四代无痕修复」「非遗」「老字号」——标题同样受平台约束）
    text, _hits = compliance.sanitize(text, contact_mode="revise")

    # ① 去品牌名（标题不需要品牌，去掉常刚好放得下）
    for b in BRANDS:
        if b in text:
            t2 = text.replace(b, "")
            if len(t2) <= LIMIT:                    # 去掉后本身就够短 → 完整
                return title_context.normalize_title(t2), "去品牌名"
    # ② 标点压缩（问号 / 冒号 / 分句边界）
    n = title_context.normalize_title(text)
    if len(n) <= LIMIT:
        return n, "标点压缩"
    # ③ 再去品牌名 + 压缩
    for b in BRANDS:
        if b in text:
            n2 = title_context.normalize_title(text.replace(b, ""))
            if len(n2) <= LIMIT:
                return n2, "去品牌名+压缩"
    return n, "硬截断"


def main() -> int:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    plan_a = []
    for r in con.execute("SELECT id, title FROM article").fetchall():
        t = r["title"] or ""
        if len(t) > LIMIT:
            nt, how = smart_shorten(t)
            plan_a.append((r["id"], t, nt, how))

    plan_t = []
    for r in con.execute(
            "SELECT id, text FROM title WHERE deleted_at IS NULL").fetchall():
        t = r["text"] or ""
        if len(t) > LIMIT:
            nt, how = smart_shorten(t)
            plan_t.append((r["id"], t, nt, how))

    print("上限 %d 字\n" % LIMIT)
    print("=== 文章（%d 篇需压缩）===" % len(plan_a))
    for i, old, new, how in plan_a:
        print("  #%-3s %2d→%2d [%s] %s" % (i, len(old), len(new), how, new))
    print("\n=== 选题（%d 条需压缩）===" % len(plan_t))
    for i, old, new, how in plan_t:
        print("  #%-3s %2d→%2d [%s] %s" % (i, len(old), len(new), how, new))

    over = [i for i, _o, new, _h in plan_a + plan_t if len(new) > LIMIT]
    print("\n压缩后仍超 %d 字：%d 条 %s" % (LIMIT, len(over), over[:5]))

    if not apply:
        print("\n（预览模式；加 --apply 写入）")
        con.close()
        return 0

    con.close()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB.with_suffix(".db.bak-title30-%s" % stamp)
    shutil.copy2(DB, bak)

    con = sqlite3.connect(DB)
    for i, _o, new, _h in plan_a:
        con.execute("UPDATE article SET title=? WHERE id=?", (new, i))
    for i, _o, new, _h in plan_t:
        con.execute("UPDATE title SET text=? WHERE id=?", (new, i))
    con.commit()
    con.close()
    (ROOT / "data" / "titles-shorten30.json").write_text(
        json.dumps({"articles": plan_a, "titles": plan_t},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已更新 文章 %d 篇 / 选题 %d 条；备份 %s"
          % (len(plan_a), len(plan_t), bak.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
