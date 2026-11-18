"""验证「页面配置的画风」是否真的替换了内置 STYLE。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_visual import STYLE, image_plan_for  # noqa: E402

CUSTOM = "【自定义画风】竖版国风水墨科普图，宣纸米色底，墨绿赭石配色"

cfg = {"style_prompt": CUSTOM,
       "image_prompts": ["玉石裂纹示意", "修复前后对比"],
       "address": "上海·全国3城就近上门",
       "cities": ["上海", "苏州", "杭州"],
       "image_count_min": 2, "image_count_max": 2,
       "copy_prompt": "", "creative_prompt": "", "banned_words": [], "phone": ""}

print("=== 1) 自定义画风 ===")
prompts, _tags = image_plan_for(cfg, "玉镯断了怎么办", "上海", 2)
for i, p in enumerate(prompts, 1):
    print("  [%d] %s" % (i, p.splitlines()[0][:70]))
    print("      含自定义画风: %s   含内置画风: %s"
          % (CUSTOM in p, STYLE[:12] in p))

print("\n=== 2) 留空 → 用内置 STYLE ===")
cfg2 = dict(cfg, style_prompt="")
prompts2, _ = image_plan_for(cfg2, "玉镯断了怎么办", "上海", 2)
for i, p in enumerate(prompts2, 1):
    print("  [%d] 含内置画风: %s" % (i, STYLE[:12] in p))

print("\n=== 3) 兜底路径变量渲染 ===")
print("  最后一条完整提示词：")
print("  " + prompts[0][:400].replace("\n", "\n  "))
