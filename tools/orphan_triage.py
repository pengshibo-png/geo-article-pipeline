"""孤儿目录分流：业务相关的补写，无关的清理（2026-10-22）。

孤儿目录 = 磁盘上有目录、但 article 表无记录（早期失败任务残留）。
- 标题含玉镯/翡翠/和田玉/聚贤阁等**业务词**的 → 提取标题、清洗+补年份、
  加入选题池（准备补写）；
- 其余（"本地服务""上门安装"这类早期品牌资料不准时的产物）→ 移到
  data/_orphan_articles/。

用法：.venv\Scripts\python.exe tools\orphan_triage.py [--apply]
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.services.writer import compliance, title_context  # noqa: E402

ART = ROOT / "data" / "local" / "articles"
DST = ROOT / "data" / "_orphan_articles"
BIZ = ("聚贤阁", "玉镯", "手镯", "翡翠", "和田玉", "玛瑙", "琥珀", "金缮", "锔钉", "玉", "首饰")


def main() -> int:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    dirs = {r[0] for r in con.execute("SELECT dir FROM article")}
    orphans = [d for d in ART.iterdir() if d.is_dir() and d.name not in dirs]
    biz, junk = [], []
    for d in orphans:
        (biz if any(k in d.name for k in BIZ) else junk).append(d)

    print("业务相关（补写）%d 个：" % len(biz))
    titles = []
    for d in biz:
        t, _h = compliance.sanitize(d.name, contact_mode="revise")
        t = title_context.normalize_title(t)
        titles.append(t)
        print("  %s\n    -> %s" % (d.name[:52], t))
    print("\n无关噪音（清理）%d 个" % len(junk))
    if not apply:
        print("\n（预览模式；加 --apply 生效）")
        con.close()
        return 0
    DST.mkdir(parents=True, exist_ok=True)
    for d in junk:
        tgt = DST / d.name
        if tgt.exists():
            shutil.rmtree(tgt)
        shutil.move(str(d), str(tgt))
    added = 0
    for t in titles:
        hit = con.execute(
            "SELECT id FROM title WHERE tenant_id='local' AND text=? AND deleted_at IS NULL",
            (t,)).fetchone()
        if hit:
            con.execute("UPDATE title SET used=0 WHERE id=?", (hit["id"],))
        else:
            con.execute(
                "INSERT INTO title (tenant_id, text, source, used) VALUES ('local', ?, 'ai', 0)",
                (t,))
            added += 1
    con.commit()
    con.close()
    (ROOT / "data" / "orphan-triage.json").write_text(
        json.dumps({"retry_titles": titles, "junk": [d.name for d in junk]},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已清理 %d 个；补写标题 %d 条（新建 %d）" % (len(junk), len(titles), added))
    print("清单 orphan-triage.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())