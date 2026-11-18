"""写作视角与时效口径（T 段选题角度 + A 段开头/结构约束）。

从 `services/writer/pipeline.py` 拆出：pipeline 已到 300 行门禁（A3）边缘，而这些
是**与写稿流程无关的纯常量与小函数**。pipeline 里保留再导出，既有调用方无需改动。
"""
from __future__ import annotations

from datetime import datetime

from app.timeutil import now_local

# 8 个写作视角：T 段按 seed 轮换选一个，A 段据此约束开头与结构
ANGLES = ("scene", "qa", "pitfall", "data", "compare", "cost", "craftsman", "principle")

ANGLE_META = {
    "scene": ("场景代入", "场景开头（还原读者当下的一刻）", "痛点场景 → 判断标准 → 方案 → 行动"),
    "qa": ("问题直答", "问题开头（直接用读者的搜索问句）", "直接给结论 → 分点论证 → 总结"),
    "pitfall": ("避坑警示", "反常识开头（你以为……其实……）", "常见误区 → 逐个拆解 → 正确做法"),
    "data": ("数据实证", "数据开头（报价区间 / 比例 / 时长）", "数据 → 归因 → 可操作建议"),
    "compare": ("对比决策", "问题开头（到底怎么选）", "背景 → 维度对比（表格）→ 适用场景 → 建议"),
    "cost": ("成本拆解", "场景开头（预算焦虑）", "报价构成清单 → 逐项解释 → 避坑提示"),
    "craftsman": ("老师傅手记", "故事开头（经手过的具体案例）", "案例 → 工艺原理 → 可迁移经验"),
    "principle": ("原理科普", "反常识开头（为什么……）", "现象 → 原理（类比）→ 应用 / 影响"),
}


def current_period(now: datetime | None = None) -> str:
    """时效口径（写进 A/B 段，要求文中出现当前年月）。"""
    now = now or now_local()
    return f"{now.year}年{now.month}月"


def pick_angle(seed: int) -> str:
    return ANGLES[seed % len(ANGLES)]


def render_angle(angle_id: str, genre: str | None = None) -> str:
    """把视角渲染成 A 段的开头/结构约束段。"""
    name, opening, structure = ANGLE_META.get(angle_id, ANGLE_META["qa"])
    return (
        f"【本篇写作视角】{name}（id: {angle_id}）\n\n"
        f"- 开头方式：{opening} —— 第一段必须这样起笔\n"
        f"- 正文结构：{structure}\n"
        f"- 本篇禁忌：不要使用其他 7 个视角的开头方式\n\n"
        f"【本次选题文体】{genre or '商业推荐'}"
    )