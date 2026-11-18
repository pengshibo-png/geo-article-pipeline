"""直接调 /api/douyin/poi-pool，看页面拿到的地区列表到底有多少。"""
from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:7001"

try:
    with urllib.request.urlopen(f"{BASE}/api/douyin/poi-pool", timeout=60) as r:
        d = json.loads(r.read().decode("utf-8"))
except Exception as exc:
    print("请求失败：", repr(exc))
    raise SystemExit(1)

regions = d.get("regions") or []
total = sum(x.get("total") or 0 for x in regions)
print("ok=%s  building=%s  built_at=%s" % (d.get("ok"), d.get("building"),
                                           d.get("built_at")))
print("接口返回地区数：%d，地址总数：%d" % (len(regions), total))
print("响应大小：%.1f MB" % (len(json.dumps(d, ensure_ascii=False)) / 1024 / 1024))
print()
print("前 10 个地区：")
for x in regions[:10]:
    print("  %-28s %3d 条" % (x.get("name"), x.get("total")))
print()
print("新疆/云南/福建 是否在返回里：")
for probe in ("乌鲁木齐市天山区", "昆明市五华区", "福州市鼓楼区", "石河子市",
              "迪庆藏族自治州香格里拉市"):
    hit = [x for x in regions if x.get("name") == probe]
    print("  %-24s %s" % (probe, ("在，%d 条" % hit[0]["total"]) if hit else "不在"))
