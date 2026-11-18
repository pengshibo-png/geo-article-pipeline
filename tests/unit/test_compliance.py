"""平台合规硬校验的单元测试（compliance.sanitize / platform_block）。

对应 2026-10-22 的需求：发布到网易号/搜狐号/百家号/头条号时，
正文不得出现手机号等联系方式，不得用绝对化用语（广告法）。
"""
from __future__ import annotations

from app.services.writer import compliance


def run(html: str, **kw):
    return compliance.sanitize(html, **kw)


def test_removes_mobile_and_landline():
    html = "<p>有需要可以拨打 13812345678 或 0571-88889999 咨询。</p>"
    out, hits = run(html, contact_mode="revise")
    assert "13812345678" not in out
    assert "0571-88889999" not in out
    assert any("电话" in h for h in hits)


def test_removes_wechat_qq_qr():
    html = "<p>加微信 zhangsan_2026 发图，QQ：88886666，或扫码咨询。</p>"
    out, hits = run(html, contact_mode="revise")
    assert "zhangsan_2026" not in out
    assert "88886666" not in out
    assert any("微信" in h for h in hits)
    assert any("QQ" in h for h in hits)
    assert any("导流" in h for h in hits)


def test_removes_tel_href_attribute():
    """html_sanitize 放行 a/href 且 _safe_url 不拦 tel:，号码会从属性漏出去。"""
    html = '<p><a href="tel:13812345678">点此联系</a></p>'
    out, hits = run(html, contact_mode="revise")
    assert "tel:" not in out
    assert "13812345678" not in out
    assert "<a" in out                      # 标签结构保留
    assert any("tel" in h for h in hits)


def test_keep_mode_never_keeps_mobile():
    """2026-11 硬约束：手机号一律删除，**连品牌自己的手机号也不保留**。

    旧行为是 keep 保留 allow_phone 指定的号码（含手机号）；按用户要求收紧后，
    keep 只对座机 / 400 / 95 热线生效。
    """
    html = "<p>品牌 13800001111，另有 13900002222。</p>"
    out, hits = run(html, contact_mode="keep", allow_phone="138-0000-1111")
    assert "13800001111" not in out         # 品牌手机号也不保留
    assert "13900002222" not in out         # 其它号照删
    assert any("手机号" in h for h in hits)


def test_keep_mode_still_keeps_landline():
    """keep 仍对座机/400 生效：列表网/58 这类渠道需要留联系方式。"""
    html = "<p>座机 0571-88886666，热线 400-123-4567。</p>"
    out, _ = run(html, contact_mode="keep", allow_phone="0571-88886666")
    assert "0571-88886666" in out           # 品牌座机保留
    assert "400-123-4567" not in out        # 非品牌号照删（keep 只保品牌那一个）


def test_strip_mobile_is_publish_gate_only():
    """发布前兜底：只删手机号，座机与其余内容原样（不改写磁盘稿子）。"""
    out, hits = compliance.strip_mobile(
        '<p>电话 13800001111，座机 0571-88886666，'
        '<a href="tel:13900002222">联系</a></p>')
    assert "13800001111" not in out
    assert "13900002222" not in out
    assert "tel:" not in out
    assert "0571-88886666" in out           # 座机不动
    assert any("手机号" in h for h in hits)


def test_revise_mode_removes_brand_phone_too():
    html = "<p>品牌电话 13800001111。</p>"
    out, _ = run(html, contact_mode="revise", allow_phone="13800001111")
    assert "13800001111" not in out


def test_absolute_words_replaced():
    html = ("<p>我们是行业第一，效果最好，100% 无痕，永不脱落，保证不加价，"
            "国家级资质，零风险。</p>")
    out, hits = run(html, contact_mode="revise")
    for bad in ("最好", "100%", "永不脱落", "保证不加价", "国家级", "零风险"):
        assert bad not in out, f"{bad} 未被替换"
    assert any("绝对化" in h for h in hits)


def test_first_step_not_falsely_flagged():
    """「第一步 / 第一段」是正常表达，只拦「行业第一 / 销量第一」。"""
    html = "<p>第一步先看断口，第一段我们讲成因。</p>"
    out, hits = run(html, contact_mode="revise")
    assert "第一步" in out and "第一段" in out
    assert not any("第一" in h for h in hits)


def test_tag_structure_preserved():
    html = '<h2>小标题</h2><p class="lead">正文 <strong>重点</strong></p>'
    out, _ = run(html, contact_mode="revise")
    assert "<h2>小标题</h2>" in out
    assert 'class="lead"' in out
    assert "<strong>重点</strong>" in out


def test_clean_article_untouched():
    html = ("<h2>断口先看形状</h2><p>断成两截的镯子先看断口是否齐整，"
            "齐整的更容易对齐，具体以实物勘验为准。</p>")
    out, hits = run(html, contact_mode="revise")
    assert out == html
    assert hits == []


def test_platform_block_mentions_four_platforms_and_rules():
    block = compliance.platform_block()
    assert "网易号" in block and "搜狐号" in block
    assert "百家号" in block and "头条号" in block
    assert "手机号" in block and "绝对化" in block


def test_negative_context_preserved():
    """否定语境里的绝对化词必须保留原样。

    「不承诺『绝对不出问题』」「不夸口说永久不坏」是**诚实表述**，替换掉会把
    意思改反（"不夸口说永久不坏" → "不夸口说耐久性较好"），也判不成违规。
    """
    out, hits = run(
        "<p>这套动作不承诺“绝对不出问题”，也不夸口说永久不坏，不承诺百分百无痕。</p>",
        contact_mode="revise")
    assert "绝对" in out
    assert "永久不坏" in out
    assert "百分百无痕" in out
    assert not any("绝对化" in h for h in hits)


def test_negative_context_keeps_cta_wording():
    """否定语境的导流话术也不能被改（「不要打电话」不能变「不要官方渠道」）。"""
    out, _hits = run("<p>不要打电话咨询，按流程寄修即可。</p>",
                          contact_mode="revise")
    assert "官方渠道" not in out
    assert "打电话" in out