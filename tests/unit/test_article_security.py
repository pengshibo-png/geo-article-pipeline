"""文章落盘链路的安全与隔离回归（2026-10-17 全项目对抗审查）。

四条通路，每条锁一遍；都是"代码看起来对、实际漏了"的类型，所以必须有测试钉住：

1. **存储型 XSS**：`01_draft.html` / `03_final.html` 是模型输出**原样落盘**的，
   档案页又把文件内容当 HTML 渲染（innerHTML）→ 正文里的 `<script>` /
   `<img onerror>` 每次打开档案页都执行，且同源执行能顺手调「保存 AI 指令」
   「发布」这些接口。修法是写侧白名单消毒（`html_sanitize.sanitize_html`
   挂在 `clean_html` 末尾）+ 读侧 `sandbox=""` iframe。
2. **租户隔离**：可引用事实库原先挂在进程级 `_共享/` 上，内容带行业属性
   （玉石/珠宝国标与工艺分类）→ 多租户部署下租户 A 的行业事实写进租户 B 的文章。
3. **读写白名单对齐**：`04_final_review.json` 写得进盘、却不在读接口白名单里，
   定稿复评对页面完全不可见。
4. **A/C 段 business 口径**：同一篇文章的 `{business}` 在 A 段渲染成品牌主名、
   在 C 段渲染成「本地服务」，改稿提示词与被改的初稿自相矛盾。
"""
from __future__ import annotations

import pytest

from app.platform import storage
from app.tenancy import tenant_scope
from tests.conftest import T1, T2, login

TITLE = "消毒测试标题"


# ── ① 消毒器本体 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("evil", [
    '<p>正文</p><script>alert(1)</script>',
    '<p>正文</p><img src=x onerror="alert(1)">',
    '<p>正文</p><svg><script>alert(1)</script></svg>',
    '<p>正文</p><iframe src="//evil.example"></iframe>',
    '<a href="java\tscript:alert(1)">正文</a>',
    '<p style="width:expression(alert(1))">正文</p>',
    '<p style="background:url(javascript:alert(1))">正文</p>',
    '<div id="view-body" onclick="alert(1)">正文</div>',
    '<form action="//evil.example"><input name="a"><p>正文</p></form>',
])
def test_sanitizer_strips_every_injection_vector(evil):
    """黑名单删标签挡不住这些变体（实测逐条可绕过），白名单重建才行。"""
    from app.services.writer.html_sanitize import sanitize_html

    out = sanitize_html(evil)
    for bad in ("<script", "onerror", "onclick", "javascript:", "expression",
                "<svg", "<iframe", "<form", "view-body", "url("):
        assert bad not in out, f"{bad!r} 没被清掉：{out}"
    assert "正文" in out, "只该清危险内容，不该把正文一起清掉"


def test_sanitizer_keeps_legitimate_article_markup():
    """白名单必须与冻结规格同口径：规格要的 h2/p/strong/table/ul/ol 一个都不能掉。"""
    from app.services.writer.html_sanitize import sanitize_html

    html = ('<h2>标题</h2><p><strong>结论</strong>与<a href="/x">链接</a></p>'
            '<table><tr><td colspan="2">单元</td></tr></table>'
            '<ul><li>条目</li></ul>'
            '<img src="/media/a.png" alt="" style="max-width:100%">')
    out = sanitize_html(html)
    for tag in ("<h2>", "<p>", "<strong>", "<table>", "<td", "<ul>", "<li>", "<img"):
        assert tag in out, f"{tag} 被误删：{out}"
    assert 'colspan="2"' in out and "max-width: 100%" in out


def test_sanitizer_is_idempotent():
    """清洗过的内容再洗一遍必须一模一样 —— 否则每次重跑都在悄悄改正文。"""
    from app.services.writer.html_sanitize import sanitize_html

    once = sanitize_html('<h2>标题</h2><p>正文 &amp; 更多</p><script>x</script>')
    assert sanitize_html(once) == once


def test_generated_draft_on_disk_has_no_active_content(app):
    """端到端：模型在正文里塞脚本 → 落盘的初稿里绝不能带可执行内容。"""
    from app.services.llm import LLMClient
    from app.services.llm.keypool import KeyPool
    from app.services.llm.providers import FakeProvider
    from app.services.writer.pipeline import generate_article

    evil = ('<h2>标题</h2><p>正文内容</p>'
            '<script>fetch("/api/my-prompts/A",{method:"POST"})</script>'
            '<img src=x onerror="alert(document.cookie)">')
    client = LLMClient(KeyPool(["k-test"]), FakeProvider(lambda m: evil), max_retries=0)

    with app.app_context(), tenant_scope(T1):
        out = generate_article(title_text=TITLE, llm=client)
        raw = storage.read_article_file(out["dir"], storage.DRAFT)

    assert "<script" not in raw and "onerror" not in raw, "落盘的初稿仍可执行脚本"
    assert "正文内容" in raw, "文章本身必须还在（消毒不等于把稿子丢掉）"


# ── ② 租户隔离：可引用事实库 ────────────────────────────────────────────────

def test_citable_facts_are_tenant_scoped(app):
    """事实库必须跟租户走：别的租户的文件不能进这个租户的 Prompt。"""
    from app.services.writer.prompt_builder import citable_facts
    from app.spec import citable_facts_file, legacy_citable_facts_file

    with app.app_context(), tenant_scope(T1):
        path = citable_facts_file()
        assert path.as_posix().endswith(f"{T1}/spec/citable-facts.md"), \
            f"事实库没有按租户落位：{path}"
        assert path != legacy_citable_facts_file(), "不能还指向旧的共享目录"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<!-- 说明注释 -->\n【可引用】GB/T 18043", encoding="utf-8")
        text = citable_facts()
        assert "GB/T 18043" in text and "说明注释" not in text, "注释头要剥掉"

    with app.app_context(), tenant_scope(T2):
        assert citable_facts() == "", "未配置的租户只能拿到空串，不能借用别家的事实"


# ── ③ 读写白名单对齐 ────────────────────────────────────────────────────────

def test_final_review_file_is_readable_through_api(client, app):
    """写侧允许的资产，读侧必须也允许（否则定稿复评对页面不可见）。"""
    from app.extensions import db
    from app.models import Article
    from app.tenancy import TenantScopedRepository

    login(client, "alpha", "pw-alpha")
    with app.app_context(), tenant_scope(T1):
        storage.write_article_file("dir-rev", storage.FINAL, "<p>定稿</p>")
        storage.write_json("dir-rev", storage.FINAL_REVIEW,
                           {"overall_score": 88, "verdict": "需要修改"})
        TenantScopedRepository(Article).add(
            Article(dir="dir-rev", title="定稿复评样本", status="final"))
        db.session.commit()

    r = client.get("/api/article/dir-rev/04_final_review.json")
    assert r.status_code == 200
    assert "overall_score" in r.get_json()["content"], "04_final_review.json 必须读得出来"


# ── ④ A / C 段 business 口径一致 ───────────────────────────────────────────

def test_business_matches_between_a_and_c_prompts(app):
    """同一篇文章里 A 段与 C 段的 {business} 必须是同一个值。

    真实状态是 `TitleConfig.business` 为空 —— 此时 A 段回落品牌主名，而 C 段
    此前单独写成 `cfg.business or "本地服务"`，于是"C 段告诉模型你在本地服务行业，
    它正在改的初稿却是按珠宝修复写的"。
    """
    from app.extensions import db
    from app.models import TitleConfig
    from app.services.writer.pipeline import build_a_variables
    from app.services.writer.review import build_c_variables
    from app.tenancy import TenantScopedRepository

    with app.app_context(), tenant_scope(T1):
        cfg = TenantScopedRepository(TitleConfig).first()
        cfg.business = ""
        db.session.commit()
        a = build_a_variables("杭州断镯修复哪家好？")
        c = build_c_variables("杭州断镯修复哪家好？", "<p>初稿</p>", "{}")

    assert a["business"] == c["business"], "A/C 两段的业务定位又分叉了"
    assert a["business"] == "阿尔法珠宝", "空配置时应回落到品牌主名"
    assert "本地服务" not in a["business"]


# ── ⑤ 全站安全响应头 ────────────────────────────────────────────────────────

def test_security_headers_present(client):
    """nosniff 拦住"上传的素材被当 HTML 执行"，另三条 CSP 指令堵住注入放大器。

    故意**没有** script-src：页面全靠内联脚本（67 个 script 块 + 389 个行内
    onclick），写了就要么打断界面要么形同虚设 —— 脚本防线在写侧消毒与读侧沙箱。
    """
    r = client.get("/healthz")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "same-origin"
    csp = r.headers["Content-Security-Policy"]
    for directive in ("object-src 'none'", "base-uri 'self'", "form-action 'self'"):
        assert directive in csp