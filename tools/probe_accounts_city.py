"""列出所有抖音账号 + 实测出口城市，并写一份 JSON 到 data/accounts-city.json。"""
from __future__ import annotations

import json
import subprocess
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "accounts-city.json"

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
out = subprocess.run([exe, "accounts", "list", "--all", "--json"],
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace").stdout
data = json.loads(out).get("data")
accs = data if isinstance(data, list) else ((data or {}).get("dataList") or [])
dy = [a for a in accs if "抖音" in str(a.get("platformName"))]

rows = []
print("抖音号 %d 个" % len(dy))
print("%-26s%-16s%-18s%s" % ("账号", "实测出口", "配置的代理", "id"))
print("-" * 88)
for a in dy:
    aid = a.get("id")
    pi = a.get("proxyInfo") or {}
    cfg = str(pi.get("name") or "").replace("|默认代理", "")
    cities = []
    try:
        r = subprocess.run([exe, "query", "locations", aid, "--query", "商场", "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=45)
        d = json.loads(r.stdout or "{}")
        for it in ((d.get("data") or {}).get("dataList") or []):
            c = ((it.get("raw") or {}).get("address_info") or {}).get("city")
            if c and c not in cities:
                cities.append(c)
    except Exception as exc:
        cities = ["ERR:" + repr(exc)[:16]]
    got = "/".join(cities) or "(空)"
    rows.append({"id": aid, "name": a.get("platformAccountName"),
                 "city": got, "configured": cfg})
    print("%-26s%-16s%-18s%s" % (str(a.get("platformAccountName"))[:24],
                                 got[:14], cfg[:16], aid))
    time.sleep(0.4)

OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n已写出:", OUT)
