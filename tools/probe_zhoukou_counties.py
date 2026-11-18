"""周口 6 个远县：多种关键词变体试探，结果写 data/zhoukou-probe.json。"""
from __future__ import annotations

import json
import subprocess
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_filter import is_bad, is_target_poi  # noqa: E402

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
ACC = "6ae4185f9dc04c194bfd2bb8"
COUNTIES = ["淮阳区", "扶沟县", "沈丘县", "郸城县", "太康县", "鹿邑县"]
OUT = ROOT / "data" / "zhoukou-probe.json"


def variants(name: str) -> list[str]:
    base = name.replace("区", "").replace("县", "")
    return [base, base + "商场", base + "广场", base + "购物中心", name]


result = {}
for c in COUNTIES:
    print("=== %s ===" % c)
    found = []
    for q in variants(c):
        try:
            r = subprocess.run([exe, "query", "locations", ACC, "--query", q, "--json"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=45)
            d = json.loads(r.stdout or "{}")
            items = ((d.get("data") or {}).get("dataList") or [])
        except Exception:
            continue
        for it in items:
            ai = ((it.get("raw") or {}).get("address_info")) or {}
            if str(ai.get("district") or "") != c:
                continue
            name = str(it.get("yixiaoerName") or "")
            entry = {"name": name, "raw": it.get("raw") or {}}
            if is_bad(name) or not is_target_poi(entry):
                continue
            if name not in found:
                found.append(name)
        time.sleep(0.3)
    result[c] = found
    print("   可用 %d 条: %s" % (len(found), "、".join(found[:5]) or "无"))

OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n已写出:", OUT)
