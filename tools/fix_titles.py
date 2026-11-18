"""批量校正选题库标题：补当前年份 + 压到平台长度上限内（2026-10-22）。

背景：
- 存量选题建库时 T 段 Prompt **没有年份变量**，94 条标题一条都没年份 → GEO 抓不到时效信号；
- 平台（网易号/头条号）把「标题过长 / 不完整」判低质降展现，长标题要压到 30 字内；
- `normalize_title` 的顺序是「先补年份 → 超长则摘掉年份、精简正文、再补回」，
  保证最终标题**既有年份又不超长**。

冲突处理：`title` 有 (tenant, text, deleted_at) 唯一约束。两条不同标题精简后可能撞成
同一条 —— 这种**跳过并报告**，不覆盖、不删除。
注意：已 used 的标题改了文本，历史文章目录名仍是旧 slug（不影响文章，只影响列表展示）。

用法：.venv\\Scripts\\python.exe tools\\fix_titles.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
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
        "SELECT id, text, used FROM title WHERE tenant_id='local' AND deleted_at IS NULL "
        "ORDER BY id").fetchall()

    plan, unchanged, junk = [], 0, []
    for r in rows:
        t = (r["text"] or "").strip()
        # 测试/垃圾标题（冒烟、占位、纯问号）：软删而不是补年份
        if (not t) or re.fullmatch(r"[?\s]+", t) or any(
                k in t for k in ("冒烟", "测试标题", "[FAKE]")):
            junk.append((r["id"], t))
            continue
        new = title_context.normalize_title(r["text"])
        if new == r["text"]:
            unchanged += 1
        else:
            plan.append((r["id"], r["text"], new, r["used"]))

    print(f"选题库有效标题 {len(rows)} 条：需改 {len(plan)} 条，不变 {unchanged} 条，"
          f"垃圾待删 {len(junk)} 条\n")
    if junk:
        print("=== 测试/垃圾标题（将软删）===")
        for tid, t in junk:
            print(f"  #{tid}  {t!r}")
        print()
    print(f"{'id':>4}  {'原（{len}）':<40}→ 新")
    for i, (tid, old, new, used) in enumerate(plan, 1):
        flag = " [已用]" if used else ""
        print(f"{tid:>4}  {old[:38]:<40}→ {new}{flag}")
        if i >= 30:
            print(f"  …… 其余 {len(plan) - 30} 条同类，省略显示")
            break

    if not plan and not junk:
        con.close()
        return 0
    if not a.apply:
        print("\n（预览模式；加 --apply 生效）")
        con.close()
        return 0

    bak = ROOT / "data" / "titles.before-year.json"
    bak.write_text(json.dumps(
        [{"id": t, "text": o} for t, o, _, _ in plan], ensure_ascii=False, indent=1),
        encoding="utf-8")

    done, conflicts = 0, []
    for tid, old, new, _used in plan:
        try:
            con.execute("UPDATE title SET text=? WHERE id=? AND tenant_id='local'",
                        (new, tid))
            con.commit()
            done += 1
        except sqlite3.IntegrityError:
            con.rollback()
            conflicts.append((tid, new))
    # 垃圾标题软删（不物理删，可回滚）
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for tid, _t in junk:
        con.execute("UPDATE title SET deleted_at=? WHERE id=? AND tenant_id='local' "
                    "AND deleted_at IS NULL", (now, tid))
    con.commit()
    con.close()
    print(f"\n已更新 {done} 条；软删垃圾 {len(junk)} 条；冲突跳过 {len(conflicts)} 条")
    for tid, new in conflicts:
        print(f"  冲突 #{tid} → {new}")
    print(f"备份：{bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
