"""清洗 POI 池：删掉不符合「商场 / 中高端小区」的条目（2026-11-01）。

判定：`is_bad(name)` 或 `not is_target(name)` → 从池中移除；键变空则删键。
改动前自动备份。

用法：.venv\\Scripts\\python.exe tools\\clean_poi_pool.py [--apply]
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_poi_filter import is_bad, is_target_poi  # noqa: E402

POOL = ROOT / "data" / "local" / "douyin" / "poi_pool.json"


def main() -> int:
    apply = "--apply" in sys.argv
    d = json.loads(POOL.read_text(encoding="utf-8"))
    regions = d.get("regions") or {}
    before_k, before_p = len(regions), sum(len(v) for v in regions.values())

    removed, kept_map, samples = [], {}, []
    for key, pois in regions.items():
        keep = []
        for p in pois or []:
            name = str(p.get("name") or "")
            if is_bad(name) or not is_target_poi(p):
                removed.append(name)
                if len(samples) < 15:
                    samples.append("%s（%s）" % (name, key))
                continue
            keep.append(p)
        if keep:
            kept_map[key] = keep

    after_k, after_p = len(kept_map), sum(len(v) for v in kept_map.values())
    print("清洗前: %d 键 / %d POI" % (before_k, before_p))
    print("清洗后: %d 键 / %d POI" % (after_k, after_p))
    print("删除: %d 条 POI，%d 个空键" % (len(removed), before_k - after_k))
    print("\n删除样例：")
    for s in samples:
        print("  -", s)
    if not apply:
        print("\n（预览模式；加 --apply 生效）")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = POOL.with_suffix(".json.bak-clean-%s" % stamp)
    shutil.copy2(POOL, bak)
    d["regions"] = kept_map
    POOL.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    print("\n已写回；备份:", bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
