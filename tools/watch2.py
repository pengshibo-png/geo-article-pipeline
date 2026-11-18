import json, sys, time, urllib.request
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "logs" / "watch2.log"

def log(m):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), m)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

log("=== 盯第二批（24 篇）===")
deadline = time.time() + 60 * 60
last = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen("http://127.0.0.1:7001/api/status", timeout=20) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        log("status 失败 %r" % e)
        time.sleep(15)
        continue
    p = d.get("progress") or {}
    cur = (p.get("done"), p.get("total"), p.get("success"), p.get("fail"), d.get("current"))
    if cur != last:
        log("done=%s/%s success=%s fail=%s | %s" % (cur[0], cur[1], cur[2], cur[3], str(cur[4])[:36]))
        last = cur
    if not d.get("running"):
        log("=== 第二批结束 ===")
        break
    time.sleep(15)