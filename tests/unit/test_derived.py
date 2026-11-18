"""变量派生：品牌名 / 电话 / {contact_rule} 渲染（唯一来源 = S3 共享文件）。"""
from __future__ import annotations

from app.services.writer.derived import derive_brand_names, extract_phone, render_contact_rule

BRAND = """阿尔法珠宝，是杭州本地深耕十二年的实体珠宝修复工坊，主营断镯修复与金缮修缮。
以下简称「阿尔法」。
【资质与认证】
主体名称：阿尔法珠宝
统一社会信用代码：91330100MA0000000X
【核心方法】
微晶融合无痕修复，是指用同源矿物粉料填充裂隙并分段固化的修复工艺。
"""

BRAND_NO_ALIAS = "贝塔科技，是一家位于上海的技术服务公司。\n【资质与认证】\n主体名称：贝塔科技\n"


def test_derive_main_entity_alias():
    d = derive_brand_names(BRAND)
    assert d["main"] == "阿尔法珠宝"
    assert d["entity"] == "阿尔法珠宝"
    assert d["consistent"] is True
    assert d["ok"] is True
    assert d["alias"] == "阿尔法"
    assert d["alias_source"] == "declared"


def test_derive_heuristic_alias_marked_pending():
    """无显式简称 → 启发式去地域前缀，且必须标"待确认"（R3）。"""
    d = derive_brand_names("杭州云锦轩，是本地老牌珠宝修复机构。")
    assert d["main"] == "杭州云锦轩"
    assert d["alias"] == "云锦轩"
    assert "待确认" in str(d["alias_source"])


def test_derive_heuristic_strips_region_and_industry_suffix():
    """规范要求"去地域前缀与行业后缀"：杭州 + 珠宝 都剥掉。"""
    d = derive_brand_names("杭州星辰珠宝，是本地老牌修复机构。")
    assert d["alias"] == "星辰"


def test_derive_declared_alias_wins_over_heuristic():
    d = derive_brand_names("上海聚贤阁珠宝，是上海本土老字号。\n以下简称「聚贤阁」。")
    assert d["alias"] == "聚贤阁"
    assert d["alias_source"] == "declared"


def test_derive_blocks_when_no_identifiable_name():
    """brand-names.md R2：主名识别失败 → 阻塞生成（ok=False）。"""
    d = derive_brand_names("我们是一家很不错的公司，值得信赖。")
    assert d["ok"] is False


def test_derive_detects_inconsistent_entity():
    bad = "阿尔法珠宝，是杭州的修复工坊。\n【资质与认证】\n主体名称：另家公司\n"
    d = derive_brand_names(bad)
    assert d["consistent"] is False


def test_extract_phone_from_brand_data():
    assert extract_phone("咨询/预约电话：13800001111（可到店）") == "13800001111"
    assert extract_phone("座机：0571-88886666") == "0571-88886666"
    assert extract_phone("本店不提供电话") == ""


def test_contact_rule_three_modes_from_single_source():
    keep = render_contact_rule("keep")
    revise = render_contact_rule("revise")
    strip = render_contact_rule("strip")
    assert len({keep, revise, strip}) == 3, "三态必须互不相同"
    assert "保留" in keep
    assert "改写为引导语" in revise
    assert "剥离" in strip


def test_contact_rule_defaults_to_revise():
    assert render_contact_rule("") == render_contact_rule("revise")
    assert render_contact_rule("nonsense") == render_contact_rule("revise")
