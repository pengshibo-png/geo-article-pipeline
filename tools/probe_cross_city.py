"""探测：用 A 城市的账号能否搜到同省 B 城市的 POI（决定"每省一号"还是"每城一号"）。"""
from __future__ import annotations

import json
import subprocess
import shutil
import time

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
DY = "6ad8992ae01687755065c7fb"      # 店铺消费实测中心（德阳）
QUERIES = ["成都锦江区商场", "成都春熙路", "锦江区商场", "成都"]

for q in QUERIES:
    print("=== 德阳号 搜「%s」===" % q)
    try:
        r = subprocess.run([exe, "query", "locations", DY, "--query", q, "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=45)
        d = json.loads(r.stdout or "{}")
        items = ((d.get("data") or {}).get("dataList") or [])
        print("   条数:", len(items))
        for it in items[:5]:
            ai = (it.get("raw") or {}).get("address_info") or {}
            print("     %-18s %s%s%s" % (str(it.get("yixiaoerName"))[:16],
                                         ai.get("province"), ai.get("city"),
                                         ai.get("district")))
    except Exception as exc:
        print("   ERR", repr(exc)[:60])
    time.sleep(0.6)
