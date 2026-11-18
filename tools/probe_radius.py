"""探测单个账号的覆盖半径：用上海号搜远郊区县，看能否命中。"""
from __future__ import annotations

import json
import subprocess
import shutil
import time

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
SH = "6ad17a6dfb87f7a2682bfe9e"      # 修镯实录（上海市）
QUERIES = ["黄浦区商场", "松江区商场", "崇明区商场", "金山区商场",
           "青浦区商场", "奉贤区商场"]

for q in QUERIES:
    print("=== 上海号 搜「%s」===" % q)
    try:
        r = subprocess.run([exe, "query", "locations", SH, "--query", q, "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=45)
        d = json.loads(r.stdout or "{}")
        items = ((d.get("data") or {}).get("dataList") or [])
        dists = []
        for it in items:
            ai = (it.get("raw") or {}).get("address_info") or {}
            dd = ai.get("district") or ""
            if dd and dd not in dists:
                dists.append(dd)
        print("   条数: %-3d 命中区: %s" % (len(items), "、".join(dists) or "(空)"))
    except Exception as exc:
        print("   ERR", repr(exc)[:60])
    time.sleep(0.6)
