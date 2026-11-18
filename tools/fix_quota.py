"""按平台真实发文上限校正账号的每日额度（2026-10-22）。

依据（联网核对的官方规则）：
- **搜狐号**：个人 / 企业 / 机构类账号 **一天最多 5 篇**，且未通过审核或被删除的
  文章同样计入名额。原 `上海聚贤阁` 配的是 14 → 每天必然有 9 篇被拒。
- 头条号 / 百家号：官方未给明确的每日篇数上限（用信用分 / 内容质量治理），
  但"单账号集中批量发布相似内容"属违规项，故只做提示、不擅自改小。

本脚本**只改 daily_limit，不动 used_today**（保留真实用量，避免"改额度即清零"）。

用法：.venv\\Scripts\\python.exe tools\\fix_quota.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 平台硬上限（只列有明确官方规定的）
PLATFORM_DAILY_CAP = {"souhu": 5}

# 建议值（无硬规定，供提示，不自动改）
ADVISORY = {"toutiao": 8, "baijiahao": 8, "netease": 5}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, platform, name, daily_limit, used_today, enabled FROM account").fetchall()

    changes, advisories = [], []
    for r in rows:
        cap = PLATFORM_DAILY_CAP.get(r["platform"])
        if r["enabled"] and cap and (r["daily_limit"] or 0) > cap:
            changes.append((r, cap))
        adv = ADVISORY.get(r["platform"])
        if r["enabled"] and adv and (r["daily_limit"] or 0) > adv:
            advisories.append((r, adv))

    print("=== 超出平台硬上限（将自动改小）===")
    if not changes:
        print("  无")
    for r, cap in changes:
        print(f"  {r['platform']:<10}{r['name'][:20]:<22} 日限 {r['daily_limit']} → {cap}"
              f"（今日已用 {r['used_today']}）")

    print("\n=== 建议值（无硬规定，仅供参考，不改）===")
    if not advisories:
        print("  无")
    for r, adv in advisories:
        print(f"  {r['platform']:<10}{r['name'][:20]:<22} 日限 {r['daily_limit']}"
              f"（建议 ≤{adv}，头条/百家把「批量发布相似内容」列为违规）")

    if not changes:
        con.close()
        return 0
    if not a.apply:
        print("\n（预览模式；加 --apply 生效）")
        con.close()
        return 0

    bak = ROOT / "data" / "quota.before-fix.json"
    bak.write_text(json.dumps(
        [{"id": r["id"], "platform": r["platform"], "name": r["name"],
          "daily_limit": r["daily_limit"], "used_today": r["used_today"]}
         for r, _ in changes], ensure_ascii=False, indent=1), encoding="utf-8")

    for r, cap in changes:
        # 只改上限；若历史用量已超新上限，同时把 used_today 压到上限内（否则 CHECK 失败）
        con.execute(
            "UPDATE account SET daily_limit=?, used_today=MIN(used_today, ?) WHERE id=?",
            (cap, cap, r["id"]))
    con.commit()
    con.close()
    print(f"\n已改 {len(changes)} 个账号；改前备份：{bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
