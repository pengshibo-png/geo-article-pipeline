"""品牌完整度算法单测（纯函数，无需 app/DB）。

**这些用例的作用是钉死口径**：主指标 100% / 原算法 89% / 要素 76%
这三个数字来自 S3《品牌资料九大分区规范》对真实资料的实测，
若算法被改坏，此处必须红——完整度是运营判断资料质量的唯一依据。
"""
from __future__ import annotations



import pytest

from app.services import brand_completeness as bc
from app.spec import brand_sections_dir


def _real_sample() -> str:
    """真实样本路径走 spec_dir()，不自己拼——与运行时同一条定位链。"""
    p = brand_sections_dir() / "_样例" / "示例品牌资料-聚贤阁珠宝.txt"
    if not p.is_file():
        pytest.skip(f"真实样本缺失：{p}")
    return p.read_text(encoding="utf-8")


# ── 解析 ────────────────────────────────────────────────────────────────────

def test_parse_splits_blocks_and_intro():
    p = bc.parse_brand("开头散句\n【资质与认证】\n主体名称：甲公司\n【品牌总结】\n总结正文")
    assert p.order == ["资质与认证", "品牌总结"]
    assert p.intro == "开头散句"
    assert "甲公司" in p.blocks["资质与认证"]


def test_section_matches_suffixed_header():
    """修正项 M2：`【核心方法·技术·工艺】` 应命中「核心方法」。"""
    p = bc.parse_brand("【核心方法·技术·工艺】\n微晶融合工艺，含步骤一、步骤二。")
    assert "微晶融合" in p.section("核心方法")


def test_overview_falls_back_to_intro():
    """修正项 M1：无【品牌概述】块时用首块前正文兜底（原系统缺陷 D-1）。"""
    p = bc.parse_brand("上海聚贤阁珠宝，是一家珠宝修复工坊。\n【品牌总结】\n总结。")
    assert p.section("品牌概述") == "上海聚贤阁珠宝，是一家珠宝修复工坊。"


def test_unknown_sections_reported_not_blocked():
    p = bc.parse_brand("【非遗传承人编号】\n内容")
    assert p.unknown_sections() == ["非遗传承人编号"]


# ── 完整度 ──────────────────────────────────────────────────────────────────

def test_completeness_on_real_sample_matches_spec():
    """口径基准：真实资料 → 主指标 100% / 原算法 89% / 要素 76%。"""
    r = bc.analyze(_real_sample())
    assert r["completeness"] == 100
    assert r["completeness_original"] == 89  # 原系统算法含缺陷 D-1
    assert r["element_completeness"] == 76
    assert r["filled"] == 9 and r["section_total"] == 9
    assert r["missing"] == []


def test_original_algorithm_misses_overview():
    """D-1 复现：无字面「品牌概述」四字时原算法必然漏算。"""
    assert bc.completeness_original("【品牌总结】" + "甲" * 100) < 100
    assert bc.completeness_original("【品牌概述】" + "甲" * 100) > 0


def test_short_section_counts_unfilled():
    """M3：按分区块阈值判定，而非关键词后 60 字。"""
    r = bc.analyze("【核心方法】\n太短")
    assert not next(s for s in r["sections"] if s["name"] == "核心方法")["filled"]


def test_empty_text_is_all_unfilled():
    r = bc.analyze("")
    assert r["completeness"] == 0
    assert len(r["missing"]) == 9


def test_none_text_is_safe():
    assert bc.analyze(None)["completeness"] == 0


# ── 派生变量 ────────────────────────────────────────────────────────────────

def test_derive_main_and_entity_from_real_sample():
    r = bc.analyze(_real_sample())
    names = r["derived"]["brand_names"]
    assert names["main"] == "上海聚贤阁珠宝"
    assert names["entity"] == "上海聚贤阁珠宝"
    assert names["confirmed"] is True
    assert names["entity_mismatch"] is False


def test_derive_phone_from_real_sample():
    """S3 发现：电话本就在资料里，A 段硬编码的根源是没抽成变量。"""
    r = bc.analyze(_real_sample())
    assert r["derived"]["phone"] == "13800001111"


def test_entity_mismatch_is_flagged():
    """主体名与主名不一致 = 阻塞项（brand-names.md §4 同源约束）。"""
    r = bc.analyze("上海聚贤阁珠宝，是一家工坊。\n【资质与认证】\n主体名称：别家珠宝\n")
    assert r["derived"]["brand_names"]["entity_mismatch"] is True


def test_heuristic_alias_marked_pending():
    """启发式派生的简称必须标「待确认」，不能冒充显式声明。"""
    r = bc.analyze(_real_sample())
    assert r["derived"]["brand_names"]["alias_source"] == "启发式(待确认)"


def test_long_first_sentence_fails_extraction():
    """首句是描述性短语而非名称 → 提取失败（应阻塞生成）。

    这条正是"19 字能蒙混过20 字阈值"的回归用例，故用描述性开头 + 短长度双重压测。
    """
    names = bc._derive_names("我们是一家提供专业珠宝修复服务的工坊，历史悠久。", "")
    assert names.main == ""
    assert names.confirmed is False


# ── 回归：单元素元组陷阱 ────────────────────────────────────────────────────

@pytest.mark.parametrize("section", list(bc.SECTION_NAMES))
def test_every_section_has_element_rules(section):
    """曾经的 bug：`(r"...")` 漏逗号被当字符串逐字符迭代，要素分静默算错。"""
    rules = bc.SECTION_ELEMENT_HINTS[section]
    assert isinstance(rules, tuple)
    assert all(isinstance(r, str) and r for r in rules)


# ── 规格一致性（G10：派生型变量唯一来源）────────────────────────────────────

def test_runtime_constants_match_frozen_spec():
    """运行时常量必须与 S3 冻结的 brand-sections.json 逐字一致。

    不一致时 CI 门禁与线上服务会给出两套不同的完整度，而运营只有一条依据
    ——故此项必须为绿。这是防止「改了代码忘了改规格」的唯一锁。
    """
    assert bc.verify_spec_consistency() == []