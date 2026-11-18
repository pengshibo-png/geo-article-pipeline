"""针对「已配了目标城市代理」的账号，重复探测出口城市（代理生效有延迟）。"""
from __future__ import annotations

import json
import subprocess
import shutil
import time

exe = shutil.which("yxer") or shutil.which("yxer.cmd")
TARGETS = ["北京", "重庆", "成都", "西安", "天津", "河南", "湖北", "湖南",
           "江西", "广东", "福建", "云南", "新疆"]

out = subprocess.run([exe, "accounts", "list", "--all", "--json"],
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace").stdout
data = json.loads(out).get("data")
accs = data if isinstance(data, list) else ((data or {}).get("dataList") or [])
dy = [a for a in accs if "抖音" in str(a.get("platformName"))]


def probe(aid):
    try:
        r = subprocess.run([exe, "query", "locations", aid, "--query", "商场", "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=45)
        d = json.loads(r.stdout or "{}")
        cities = []
        for it in ((d.get("data") or {}).get("dataList") or []):
            c = ((it.get("raw") or {}).get("address_info") or {}).get("city")
            if c and c not in cities:
                cities.append(c)
        return "/".join(cities) or "(空)"
    except Exception as exc:
        return "ERR:" + repr(exc)[:20]


print("%-24s%-16s%-16s%s" % ("账号", "配置的代理", "实测出口", "一致"))
print("-" * 76)
hit = []
for a in dy:
    pi = a.get("proxyInfo") or {}
    cfg = str(pi.get("name") or "").replace("|默认代理", "")
    if not any(t in cfg for t in TARGETS):
        continue
    got = probe(a.get("id"))
    ok = "OK" if cfg.split("-")[-1].replace("市", "") in got else "no"
    if ok == "OK":
        hit.append((a.get("id"), a.get("platformAccountName"), got, cfg))
    print("%-24s%-16s%-16s%s" % (str(a.get("platformAccountName"))[:22], cfg[:14],
                                 got[:14], ok))
    time.sleep(0.8)

print("\n可用的目标城市账号：", len(hit))
for aid, nm, got, cfg in hit:
    print("  %s | %s | %s" % (aid, nm, got))
