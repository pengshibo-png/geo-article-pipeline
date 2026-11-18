"""按「城市 → 账号」映射批量抓 POI（2026-11-01）。

用法：.venv\\Scripts\\python.exe tools\\build_poi_batch.py [--cities 重庆市,成都市]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
TOOL = str(ROOT / "tools" / "build_poi_city.py")

# 实测出口城市 → 账号（2026-11-01 探测结果）
CITY_ACCOUNT = {
    "北京市": "6a72f808ee584439c1498a48",   # 老王全国实测日记
    "重庆市": "6a72f7195ef68d5087f51d7e",   # 上海聚贤阁玉石修复
    "成都市": "6aadf81ed033d36c0b7dfcd8",   # 玉见真实
    "西安市": "6adb276ce353fba14f3a7e81",   # 不吹不黑测评
    "深圳市": "6ad17a6dfb87f7a2682bfe9e",   # 修镯实录
    "天津市": "6ae3163e381ee67120da7dda",   # 真话实测
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default="")
    ap.add_argument("--pages", type=int, default=2)
    a = ap.parse_args()
    cities = ([x.strip() for x in a.cities.split(",") if x.strip()]
              if a.cities else list(CITY_ACCOUNT))

    results = []
    for city in cities:
        acc = CITY_ACCOUNT.get(city)
        if not acc:
            print("跳过（没有可用账号）：", city)
            continue
        print("\n" + "=" * 60)
        print("抓取 %s（账号 %s）" % (city, acc))
        t0 = time.time()
        p = subprocess.run([PY, TOOL, "--city", city, "--account", acc,
                            "--pages", str(a.pages), "--apply"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(ROOT))
        out = (p.stdout or "") + (p.stderr or "")
        print(out[-600:])
        cov = ""
        for line in out.splitlines():
            if "覆盖率" in line or "新增 POI" in line:
                print("  " + line.strip())
                if "覆盖率" in line:
                    cov = line.strip()
        results.append((city, round(time.time() - t0), cov))

    print("\n" + "=" * 60)
    print("汇总：")
    for city, sec, cov in results:
        print("  %-8s %3d 秒  %s" % (city, sec, cov))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
