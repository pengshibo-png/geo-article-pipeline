"""让批量写稿**精确**只跑「刚退回的那 26 条」，而不是最早的噪音选题。

背景：`managed._fresh_titles` 按 `created_at.asc(), id.asc()` 取未用选题 ——
直接 `/api/run 26` 会先取到库里更早的无关标题（"上门安装怎么选"这类）。
而批量入口没有"按标题列表"的接口。

做法：把**非目标**的未用标题临时标成 used=True（锁），跑完再解锁。
锁/解锁都写进 `data/rebuild-lock.json`，即使中途失败也能一键恢复。

用法：
    .venv\\Scripts\\python.exe tools\\queue_rebuild.py --lock
    （触发 /api/run count=<目标数>）
    .venv\\Scripts\\python.exe tools\\queue_rebuild.py --unlock
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "data" / "rebuild-lock.json"
PLAN = ROOT / "data" / "rebuild-prepare.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", action="store_true")
    ap.add_argument("--unlock", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row

    if a.lock:
        targets = {x["title"] for x in
                   json.loads(PLAN.read_text(encoding="utf-8"))["moved"]}
        rows = con.execute(
            "SELECT id, text, used FROM title WHERE tenant_id='local' "
            "AND deleted_at IS NULL AND used=0").fetchall()
        locked = [r["id"] for r in rows if r["text"] not in targets]
        free = [r["text"] for r in rows if r["text"] in targets]
        if locked:
            con.executemany("UPDATE title SET used=1 WHERE id=?",
                            [(i,) for i in locked])
        con.commit()
        LOCK.write_text(json.dumps({"locked": locked}, ensure_ascii=False), encoding="utf-8")
        print(f"目标（会被批量取到）{len(free)} 条：")
        for t in free:
            print(f"  {t[:40]}")
        print(f"\n临时锁定其它未用标题 {len(locked)} 条 → {LOCK}")
        print(f"下一步：触发 /api/run count={len(free)}")
    elif a.unlock:
        if not LOCK.exists():
            print("没有锁记录")
            con.close()
            return 0
        ids = json.loads(LOCK.read_text(encoding="utf-8")).get("locked") or []
        if ids:
            con.executemany("UPDATE title SET used=0 WHERE id=?", [(i,) for i in ids])
            con.commit()
        LOCK.unlink()
        left = con.execute(
            "SELECT COUNT(*) FROM title WHERE tenant_id='local' "
            "AND deleted_at IS NULL AND used=0").fetchone()[0]
        print(f"已解锁 {len(ids)} 条；当前未用选题 {left} 条")
    else:
        print("用 --lock 或 --unlock")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
