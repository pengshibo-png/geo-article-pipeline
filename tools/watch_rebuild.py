"""盯住批量重写直到结束，然后解锁选题并出验收报告。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer import compliance  # noqa: E402

BASE = "http://127.0.0.1:7001"
ART = ROOT / "data" / "local" / "articles"
LOG = ROOT / "data" / "logs" / "rebuild-watch.log"
REPORT = ROOT / "data" / "reports" / "rebuild-report.md"
PLAN = ROOT / "data" / "rebuild-prepare.json"


def _log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def status() -> dict:
    with urllib.request.urlopen(BASE + "/api/status", timeout=20) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=45)
    a = ap.parse_args()
    _log("=== 开始盯批量重写 ===")
    deadline = time.time() + a.minutes * 60
    last = None
    while time.time() < deadline:
        try:
            d = status()
        except Exception as exc:
            _log(f"status 失败: {exc!r}")
            time.sleep(10)
            continue
        p = d.get("progress") or {}
        cur = (p.get("done"), p.get("total"), p.get("success"), p.get("fail"),
               d.get("current"))
        if cur != last:
            _log(f"done={cur[0]}/{cur[1]} success={cur[2]} fail={cur[3]} | {str(cur[4])[:36]}")
            last = cur
        if not d.get("running"):
            _log("=== 批量结束 ===")
            break
        time.sleep(12)
    else:
        _log("!! 超时未结束")

    try:
        out = subprocess.run(
            [str(ROOT / ".venv" / "Scripts" / "python.exe"),
             str(ROOT / "tools" / "queue_rebuild.py"), "--unlock"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT))
        _log("解锁: " + (out.stdout or out.stderr or "").strip().replace("\n", " "))
    except Exception as exc:
        _log(f"解锁失败(手动跑 queue_rebuild.py --unlock): {exc!r}")

    con = sqlite3.connect(ROOT / "data" / "app.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, dir, title FROM article ORDER BY id").fetchall()
    con.close()
    targets = set()
    if PLAN.exists():
        targets = {x["dir"] for x in
                   json.loads(PLAN.read_text(encoding="utf-8"))["moved"]}

    lines = [f"# 批量重写验收报告（{datetime.now():%Y-%m-%d %H:%M}）", ""]
    ok_cnt = bad_cnt = 0
    for r in rows:
        d = ART / r["dir"]
        p = d / "03_final.html"
        if not p.exists():
            p = d / "01_draft.html"
        if not p.exists():
            continue
        html = p.read_text(encoding="utf-8")
        chars = compliance.visible_chars(html)
        miss = compliance.missing_elements(html)
        _c, hits = compliance.sanitize(html, contact_mode="revise")
        real = [h for h in hits if not h.startswith(("空", "悬空", "时效"))]
        good = (not miss) and chars >= 2000 and not real
        ok_cnt += good
        bad_cnt += (not good)
        if r["dir"] in targets:
            lines.append(
                f"- {'PASS' if good else 'WARN'} #{r['id']} {r['title'][:32]} "
                f"| {chars}字 | 缺{len(miss)}{'/' + '/'.join(miss) if miss else ''} "
                f"| 违规{len(real)}")
    lines.insert(2, f"目标 {len(targets)} 篇；达标 {ok_cnt} / 未达标 {bad_cnt}")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log(f"验收完成：达标 {ok_cnt} / 未达标 {bad_cnt} → {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
