"""四平台（网易号/搜狐号/百家号/头条号）官方规范的合规测试。

2026-10-22 联网核对官方规范后新补的六类红线：
邮箱、QQ群/微信群、第三方网址/网盘、公众号引流、标题党、低质格式符号。
每条的出处见 `compliance.platform_block()` 的 docstring。
"""
from __future__ import annotations

from app.services.writer import compliance


def run(html: str, **kw):
    return compliance.sanitize(html, **kw)


# ── 邮箱（网易号：引导添加微信、qq、手机号、**邮箱**等所有联系方式）──────────
def test_removes_email():
    out, hits = run("<p>资料发 demo@example.com 或 xu@example.com.cn</p>",
                    contact_mode="revise")
    assert "@" not in out
    assert "100000001" not in out
    assert any("邮箱" in h for h in hits)


# ── 群号（头条号：群联系方式，如 QQ群、微信群）──────────────────────────────
def test_removes_group_contacts():
    out, hits = run("<p>QQ群 88886666，微信群也可以。</p>", contact_mode="revise")
    assert "88886666" not in out
    assert any("群号" in h for h in hits)


# ── 外链/网盘（头条号：第三方网站或网址、网盘链接）──────────────────────────
def test_removes_url_and_netdisk():
    out, hits = run("<p>看 https://example.com/a 或 pan.baidu.com/s/abc 或 www.foo.cn</p>",
                    contact_mode="revise")
    assert "http" not in out
    assert "pan.baidu.com" not in out
    assert "www.foo.cn" not in out
    assert any("外链" in h for h in hits)


# ── 公众号引流（百家号：广告认定宽到「提一句公众号名」也算）────────────────
def test_removes_public_account_reference():
    out, hits = run("<p>更多案例见公众号 聚贤阁玉修 或关注百家号。</p>",
                    contact_mode="revise")
    assert "聚贤阁玉修" not in out
    assert any("公众号" in h for h in hits)


def test_url_cleanup_keeps_normal_sentence():
    """外链清理不能把正常句子里的点号当域名。"""
    out, hits = run("<p>断口齐整、无崩口的，对齐后强度更有保障。</p>",
                    contact_mode="revise")
    assert out == "<p>断口齐整、无崩口的，对齐后强度更有保障。</p>"
    assert hits == []


# ── 标题（网易号/头条号/搜狐号 都单独管标题）────────────────────────────────
def test_title_problems_flags_decorations_and_hype():
    assert "标题含装饰符号" in compliance.title_problems("★玉镯断了怎么修★")
    assert any("极限词" in p for p in compliance.title_problems("上海最好的玉镯修复"))
    assert "标题党标点" in compliance.title_problems("玉镯断了？？？怎么办！！")
    assert any("过长" in p for p in compliance.title_problems("上海" * 20))


def test_title_problems_accepts_normal_title():
    assert compliance.title_problems("上海玉镯断了两截怎么修，先看断口再定方案") == []


def test_title_rejects_absolute_claim():
    """「第一」在标题里一律算极限词（不像正文里放过「第一步」）。"""
    assert any("极限词" in p for p in compliance.title_problems("玉镯修复第一家"))


# ── 平台块必须覆盖四个平台 + 六类新增红线（防止以后漏写）────────────────────
def test_platform_block_covers_all_four_and_new_rules():
    b = compliance.platform_block()
    for name in ("网易号", "搜狐号", "百家号", "头条号"):
        assert name in b
    for kw in ("邮箱", "QQ群", "网盘", "公众号", "标题党", "虚构亲历", "未分段"):
        assert kw in b, f"平台块缺少 {kw}"
