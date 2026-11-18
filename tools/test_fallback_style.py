"""验证：**旧版**（没有 style_prompt 字段）时，只改「海报提示词兜底池」
能在多大程度上改变画风。

旧版的拼装逻辑（douyin_visual.image_plan_for 兜底路径）：
    f"{STYLE}\\n【版式】{LAYOUTS['knowledge']}\\n{head}【内容】{text}\\n{footer}"
所以内置 STYLE 始终在最前面，兜底池只填【内容】。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.douyin_visual import STYLE, image_plan_for  # noqa: E402

# 旧版配置：没有 style_prompt
BASE_CFG = {"image_prompts": [], "address": "上海·就近上门",
            "cities": ["上海"], "image_count_min": 2, "image_count_max": 2,
            "copy_prompt": "", "creative_prompt": "", "banned_words": [],
            "phone": ""}

WEAK = "玉石裂纹示意"
STRONG = ("【本条画风覆盖前面的默认描述】改用国风水墨科普风：宣纸质感米色底、"
          "墨绿与赭石配色、宋体大字、细墨线卡片、角落淡墨玉石线稿；"
          "严禁照片写实、卡通、渐变、红色促销标签。画面内容：玉石裂纹示意，"
          "用一张剖面线稿标出裂纹走向")


def show(label: str, prompts: list[str]) -> None:
    print("=== %s ===" % label)
    p = prompts[0]
    print("  [最终提示词全文]")
    for line in p.splitlines():
        print("   " + line[:110])
    print()


cfg = dict(BASE_CFG, image_prompts=[WEAK])
p1, _ = image_plan_for(cfg, "玉镯断了怎么办", "上海", 2)
show("A) 兜底池只写内容（旧版默认用法）", p1)

cfg2 = dict(BASE_CFG, image_prompts=[STRONG])
p2, _ = image_plan_for(cfg2, "玉镯断了怎么办", "上海", 2)
show("B) 兜底池里塞满画风要求", p2)

print("=== 结论对比 ===")
print("  内置 STYLE 是否仍在提示词里：", STYLE[:14] in p2[0])
print("  兜底池画风是否也进了提示词：", "宣纸质感米色底" in p2[0])
print("  → 两套画风会同时出现在 prompt 里，模型按权重取舍；")
print("    兜底池那条写得越靠后越具体，越容易压过前面的默认描述。")
