"""查看各平台发布账号的每日额度（用于核对是否超出平台发文上限）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
con = sqlite3.connect(ROOT / "data" / "app.db")
rows = con.execute(
    "SELECT platform, name, daily_limit, used_today, enabled FROM account "
    "WHERE platform IN ('souhu','toutiao','netease','baijiahao','douyin_imgtext') "
    "ORDER BY enabled DESC, platform, name").fetchall()
con.close()
print(f"{'平台':<16}{'账号':<22}{'日限':>5}{'今日':>5}  启用")
for p, n, lim, used, en in rows:
    print(f"{p:<16}{(n or '')[:20]:<22}{lim:>5}{used:>5}  {'是' if en else '否'}")
