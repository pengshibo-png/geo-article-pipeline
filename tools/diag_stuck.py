"""诊断批量写稿卡住：SQLite 模式 / 写锁 / 是否有文章在落盘。"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
db = ROOT / "data" / "app.db"
con = sqlite3.connect(db, timeout=3)
print("journal_mode :", con.execute("PRAGMA journal_mode").fetchone()[0])
print("busy_timeout:", con.execute("PRAGMA busy_timeout").fetchone()[0])
try:
    con.execute("BEGIN IMMEDIATE")
    print("写锁         : 可立即获得（无其它写事务占着）")
    con.rollback()
except Exception as exc:
    print("写锁         : 被占用 ->", exc)

print("\n=== status 接口是否还能响应 ===")
t0 = time.time()
try:
    with urllib.request.urlopen("http://127.0.0.1:7001/api/status", timeout=8) as r:
        d = json.loads(r.read().decode())
    p = d.get("progress") or {}
    print(f"响应耗时 {time.time() - t0:.1f}s | running={d.get('running')} "
          f"done={p.get('done')}/{p.get('total')} current={d.get('current')}")
except Exception as exc:
    print(f"status 失败（耗时 {time.time() - t0:.1f}s）:", repr(exc))

print("\n=== 文章目录最近修改 ===")
ART = ROOT / "data" / "local" / "articles"
now = time.time()
for d in sorted([x for x in ART.iterdir() if x.is_dir()],
                key=lambda x: x.stat().st_mtime, reverse=True)[:6]:
    print(f"  {int(now - d.stat().st_mtime):>5}s ago  {d.name[:40]}")
