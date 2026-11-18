"""实测抖音图文的 3 个提示词到底有没有真的进 prompt / 进生图提示词。

三个提示词（参数设置页从上到下）：
  ① copy_prompt      图文文案提示词   → 拼在文案 prompt 最前面
  ② creative_prompt  创意一体化指令   → 图纲 prompt 的指令（决定每张图讲什么）
  ③ style_prompt     海报画风提示词   → 每条生图提示词的**第一段**（决定画面风格）

2026-11-10 变更：原来的 `image_prompts`（海报提示词兜底池）已下线 —— 它只在
图纲规划失败时生效，用户改了看不到效果。画风改由 `style_prompt` 决定。

方法：把 `LLMClient.complete` 换成间谍，截获真正发给模型的 prompt，
检查里面有没有塞进去的测试标记。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.services import douyin_pack  # noqa: E402
from app.services.douyin_visual import image_plan_for, render_prompt  # noqa: E402
from app.services.llm import LLMClient  # noqa: E402

CAP: list[str] = []
_orig = LLMClient.complete


def _spy(self, messages, **kw):
    try:
        CAP.append(str(messages[-1].get("content") or ""))
    except Exception:
        CAP.append(str(messages))
    raise RuntimeError("__spy_stop__")      # 让它失败 → 走兜底，但 prompt 已截获


LLMClient.complete = _spy

MARK_COPY = "【测试标记-文案-9X7】"
MARK_CRE = "【测试标记-创意-9X7】"
MARK_STYLE = "【测试标记-画风-9X7】赛博朋克霓虹科技风"

BASE = {"cities": ["上海"], "phone": "", "address": "上海·就近上门",
        "banned_words": [], "image_count_min": 3, "image_count_max": 3}


def run() -> None:
    # ① copy_prompt ────────────────────────────────────────────
    cfg1 = dict(BASE, copy_prompt=MARK_COPY + " 写一段修复知识分享",
                creative_prompt="")
    CAP.clear()
    try:
        douyin_pack.copy_for(cfg1, "上海", "聚贤阁", "")
    except Exception:
        pass
    print("① copy_prompt（图文文案）")
    print("   进了 prompt：", any(MARK_COPY in c for c in CAP),
          " (截获 %d 次调用)" % len(CAP))
    if CAP:
        print("   prompt 开头：", CAP[0][:80].replace("\n", " "))

    # ② creative_prompt ────────────────────────────────────────
    cfg2 = dict(BASE, copy_prompt="", creative_prompt=MARK_CRE + " 做 3 张科普图")
    CAP.clear()
    prompts2, _t = image_plan_for(cfg2, "玉镯断了能修吗，先看断口", "上海", 3)
    print("\n② creative_prompt（图纲 / 标签）")
    print("   进了 prompt：", any(MARK_CRE in c for c in CAP),
          " (截获 %d 次调用)" % len(CAP))
    if CAP:
        print("   prompt 开头：", CAP[0][:80].replace("\n", " "))
    print("   AI 失败 → 用文案首句兜底，产出 %d 条" % len(prompts2))

    # ③ style_prompt（画风）──────────────────────────────────
    # 3a：正常路径（图纲成功）—— 画风必须是每条提示词的第一段
    slide = {"role": "cover", "headline": "玉镯断了还能戴吗",
             "cards": [{"k": "1", "t": "先看断口", "d": "崩口与暗裂工艺不同"}],
             "contrast": {"left": "多数人只看价格", "right": "内行先问工艺"},
             "tail": "以实物鉴定为准"}
    p_ok = render_prompt(slide, brand="聚贤阁", style=MARK_STYLE)
    print("\n③ style_prompt（海报画风）")
    print("   正常路径（图纲成功）第一段就是画风：", p_ok.startswith(MARK_STYLE))

    # 3b：兜底路径（图纲失败）—— 画风同样要进，且不再套内置米白风
    cfg3 = dict(BASE, copy_prompt="", creative_prompt="", style_prompt=MARK_STYLE)
    prompts3, _t = image_plan_for(cfg3, "玉镯断了能修吗", "上海", 2)
    print("   兜底路径（图纲失败）每条都带画风：", all(MARK_STYLE in p for p in prompts3))
    print("   兜底路径不再套内置风格：", all("科普信息图" not in p for p in prompts3))
    for i, p in enumerate(prompts3, 1):
        print("     [%d] %s" % (i, p.splitlines()[0][:52]))


app = create_app()
with app.app_context():
    run()

LLMClient.complete = _orig

print("\n=== 结论 ===")
print("  copy_prompt     → 拼在文案 prompt 最前面（每次生成都用）")
print("  creative_prompt → 图纲 prompt 的指令（AI 成功路径生效）")
print("  style_prompt    → 每条生图提示词的第一段（成功/兜底两条路径都生效）")
print("  image_prompts   → 已下线（只在规划失败时生效，用户改了看不到效果）")
