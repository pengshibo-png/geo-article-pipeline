"""清理孤儿文章目录：有目录、无 article 记录（历史残留，不会出现在档案页）。

这些目录多数只有 01_draft.html（任务中途失败/中断），标题还是早期品牌资料
不准时的产物（"本地服务""上门安装"这类与玉石无关的题），留着只会让人误以为
"有几篇没写完"。

移动而非删除到 data/_orphan_articles/，可恢复。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ART = ROOT / "data" / "local" / "articles"
DST = ROOT / "data" / "_orphan_articles"


def main() -> int:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(ROOT / "data" / "app.db")
    dirs = {r[0] for r in con.execute("SELECT dir FROM article")}
    con.close()
    orphans = [d for d in ART.iterdir() if d.is_dir() and d.name not in dirs]
    print("孤儿目录 %d 个：" % len(orphans))
    total_files = 0
    for d in orphans:
        n = len(list(d.iterdir()))
        total_files += n
        print("  %-46s %d 个文件" % (d.name[:44], n))
    print("\n合计 %d 个目录 / %d 个文件" % (len(orphans), total_files))
    if not apply:
        print("（预览模式；加 --apply 生效）")
        return 0
    DST.mkdir(parents=True, exist_ok=True)
    moved = []
    for d in orphans:
        tgt = DST / d.name
        if tgt.exists():
            shutil.rmtree(tgt)
        shutil.move(str(d), str(tgt))
        moved.append(d.name)
    (ROOT / "data" / "orphan-clean.json").write_text(
        json.dumps(moved, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已移到 %s；清单 orphan-clean.json" % DST)
    left = len([d for d in ART.iterdir() if d.is_dir()])
    print("剩余文章目录 %d 个（与 article 记录一致）" % left)
    return 0


if __name__ == "__main__":
    sys.exit(main())