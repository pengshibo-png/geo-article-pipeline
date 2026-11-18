"""真实渠道驱动（Playwright）测试。

**不启动浏览器**：用 StubPage 扮演 Playwright 的 page/locator，只验证
「Cookie 解析 → 驱动选择 → 平台流程 → 错误归一化」这套逻辑。
真浏览器只在 tools/publish_probe.py（人工校准）里跑。
"""
from __future__ import annotations

import pytest

from app.services.publish import browser
from app.services.publish.drivers import PublishError, get_driver
from app.services.publish.executor import platform_title
from app.services.publish.platforms import ADAPTERS, get_adapter
from app.services.publish.platforms.base import plain_text

PLATFORMS = ("meipian", "souhu", "baijiahao", "lieju", "wuba")


# ── Stub：Playwright page / locator 的最小替身 ──────────────────────────────

class StubLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.page.present(self.sel) else 0

    def wait_for(self, state=None, timeout=None):
        if not self.page.present(self.sel):
            raise RuntimeError(f"not visible: {self.sel}")

    def click(self):
        self.page.clicks.append(self.sel)
        if self.sel in self.page.submits:
            self.page.url = self.page.success_url

    def fill(self, value):
        self.page.fills.append((self.sel, value))

    def evaluate(self, script, arg=None):
        if "tagName" in script:
            return self.page.tag_for(self.sel)
        self.page.html.append(arg)
        return None

    def set_input_files(self, path, timeout=None):
        self.page.uploads.append(path)

    def select_option(self, label=None):
        self.page.selects.append((self.sel, label))

    def inner_text(self):
        return self.page.text_of(self.sel)


class StubPage:
    def __init__(self, present=(), *, url="https://x/editor", success_url=None,
                 submits=(), tags=None, texts=None, redirect=None):
        self._present = set(present) | set(texts or {})   # 文案选择器也算"存在"
        self.url = url
        self.success_url = success_url or url
        self.redirect = redirect          # 未登录时 goto 会被跳到登录页
        self.submits = set(submits)
        self.tags = tags or {}
        self.texts = texts or {}
        self.clicks, self.fills, self.html, self.uploads, self.selects = [], [], [], [], []

    def present(self, sel):
        return sel in self._present

    def tag_for(self, sel):
        return self.tags.get(sel, "DIV")

    def text_of(self, sel):
        return self.texts.get(sel, "")

    def locator(self, sel):
        return StubLocator(self, sel)

    def frame_locator(self, sel):
        return self

    def goto(self, url, wait_until=None):
        self.url = self.redirect or url

    def wait_for_timeout(self, ms):
        return None

    class _Keyboard:
        def __init__(self, page):
            self.page = page

        def insert_text(self, text):
            self.page.fills.append(("<keyboard>", text))

    @property
    def keyboard(self):
        return StubPage._Keyboard(self)


# ── Cookie ─────────────────────────────────────────────────────────────────

def test_cookie_domain_for_each_platform():
    assert browser.cookie_domain("https://mp.sohu.com/") == ".sohu.com"
    assert browser.cookie_domain("https://www.meipian.cn/editor") == ".meipian.cn"
    assert browser.cookie_domain("https://baijiahao.baidu.com/") == ".baidu.com"
    assert browser.cookie_domain("https://post.58.com/342") == ".58.com"
    assert browser.cookie_domain("https://www.lieju.com/") == ".lieju.com"


def test_parse_cookies_header_json_and_netscape():
    dom = ".meipian.cn"
    head = browser.parse_cookies("a=1; b=2", domain=dom)
    assert [(c["name"], c["value"], c["domain"]) for c in head] == [
        ("a", "1", dom), ("b", "2", dom)]

    js = browser.parse_cookies('[{"name":"t","value":"v","domain":".x.com"}]', domain=dom)
    assert js == [{"name": "t", "value": "v", "domain": ".x.com", "path": "/"}]

    net = browser.parse_cookies(
        "# Netscape HTTP Cookie File\n.meipian.cn\tTRUE\t/\tFALSE\t0\tmp_token\tabc",
        domain=dom)
    assert net == [{"name": "mp_token", "value": "abc", "domain": ".meipian.cn", "path": "/"}]
    assert browser.parse_cookies("", domain=dom) == []


# ── 驱动选择（唯一判定入口）─────────────────────────────────────────────────

class _Acct:
    def __init__(self, *, cookie=b"blob", yxer=None, id="mp-1"):
        self.id, self.cookie_blob, self.yxer_id, self.platform = id, cookie, yxer, "meipian"


def test_resolve_mode_prefers_yxer_then_cookie(app, monkeypatch):
    """判定优先级：蚁小二 > 离线开关 > 平台适配器 > Cookie 有无。"""
    monkeypatch.setattr(browser, "driver_mode", lambda: "auto")
    with app.app_context():
        assert browser.resolve_mode("meipian", _Acct(yxer="y-1"))[0] == "yxer"
        assert browser.resolve_mode("meipian", _Acct(cookie=None))[0] == "fake"
        assert browser.resolve_mode("other", _Acct())[0] == "fake"       # 未适配平台
        assert browser.resolve_mode("meipian", _Acct())[0] == "real"     # 有 Cookie → 真发
    monkeypatch.setattr(browser, "driver_mode", lambda: "fake")
    with app.app_context():
        assert browser.resolve_mode("meipian", _Acct())[0] == "fake"     # 开关优先


def test_test_env_never_uses_real_driver(app):
    """测试环境 publish_driver=fake：任何账号都走离线驱动，绝不启动浏览器。"""
    with app.app_context():
        assert browser.driver_mode() == "fake"
        driver = get_driver("meipian", _Acct())
        assert driver.__class__.__name__ == "FakeDriver"
        assert "离线" in driver.note


def test_registry_covers_five_platforms():
    assert set(ADAPTERS) == set(PLATFORMS)
    for p in PLATFORMS:
        adapter = get_adapter(p)
        assert adapter.platform == p and adapter.name
        assert adapter.cfg["home"] and adapter.cfg["editor"]
        assert adapter.cfg["title"] and adapter.cfg["body"]["selectors"]


def test_selector_overrides_merge_from_json(app, monkeypatch):
    monkeypatch.setattr(browser, "selector_overrides",
                        lambda p: {"title": ["#custom"]})
    assert get_adapter("meipian").cfg["title"] == ["#custom"]
    assert get_adapter("meipian").cfg["home"].startswith("https://www.meipian")


# ── 平台流程（StubPage）────────────────────────────────────────────────────

def test_meipian_flow_fills_title_and_body():
    adapter = get_adapter("meipian")
    page = StubPage(
        present={"input.editor-title", ".w-e-text",
                 "button:has-text('完成/分享')"},
        submits={"button:has-text('完成/分享')"},
        success_url="https://www.meipian.cn/abc123",
    )
    url = adapter.publish(page, html="<p>正文</p>", title="标题甲")
    assert url == "https://www.meipian.cn/abc123"
    assert ("input.editor-title", "标题甲") in page.fills
    assert "<p>正文</p>" in page.html


def test_textarea_platform_uses_fill_not_innerhtml():
    """列表网/58 是表单站：正文进 textarea，必须 fill 而不是写 innerHTML。"""
    adapter = get_adapter("lieju")
    page = StubPage(present={"input[placeholder*='标题']", "textarea",
                             "button:has-text('发布')"},
                    submits={"button:has-text('发布')"},
                    tags={"textarea": "TEXTAREA"},
                    texts={"text=发布成功": "发布成功"})
    adapter.publish(page, html="<p>第一段</p><p>第二段</p>", title="列表网标题")
    body = [v for sel, v in page.fills if sel == "textarea"]
    assert body and "第一段" in body[0] and "<p>" not in body[0]


def test_baijiahao_flow_writes_into_iframe():
    adapter = get_adapter("baijiahao")
    page = StubPage(present={"textarea[placeholder*='标题']", "body.view",
                             "button:has-text('发布')"},
                    submits={"button:has-text('发布')"},
                    url="https://baijiahao.baidu.com/builder/rc/edit?type=news",
                    success_url="https://baijiahao.baidu.com/builder/rc/content")
    url = adapter.publish(page, html="<p>百家号正文</p>", title="百家号标题")
    assert url.endswith("/builder/rc/content")
    assert "<p>百家号正文</p>" in page.html


def test_login_wall_reports_login_expired():
    adapter = get_adapter("baijiahao")
    page = StubPage(present={"textarea[placeholder*='标题']"},
                    redirect="https://baijiahao.baidu.com/builder/theme/bjh/login")
    with pytest.raises(PublishError) as ei:
        adapter.publish(page, html="<p>x</p>", title="t")
    assert ei.value.code == "login_expired"


def test_visible_login_link_reports_login_expired():
    """58 不跳登录页：顶部栏就地显示「登录」入口也要判失效。"""
    adapter = get_adapter("wuba")
    page = StubPage(present={"a:visible:has-text('登录'), button:visible:has-text('登录')"},
                    url="https://post.58.com/342",
                    texts={"a:visible:has-text('登录'), button:visible:has-text('登录')": "登录"})
    with pytest.raises(PublishError) as ei:
        adapter.publish(page, html="<p>x</p>", title="t")
    assert ei.value.code == "login_expired"


def test_captcha_wall_reports_captcha():
    adapter = get_adapter("souhu")
    page = StubPage(present={"#tcaptcha_iframe_dy"}, url="https://mp.sohu.com/editor")
    with pytest.raises(PublishError) as ei:
        adapter.publish(page, html="<p>x</p>", title="t")
    assert ei.value.code == "captcha"


def test_missing_selector_points_to_probe_tool():
    adapter = get_adapter("souhu")
    page = StubPage(present=set(), url="https://mp.sohu.com/editor")
    with pytest.raises(PublishError) as ei:
        adapter.publish(page, html="<p>x</p>", title="t")
    assert "publish_probe" in ei.value.message and ei.value.code == "unknown"


def test_wait_success_times_out_honestly():
    """既没有成功提示也没有跳转 → 报错（绝不假设成功）。"""
    adapter = get_adapter("souhu")
    cfg = dict(adapter.cfg)
    cfg["success"] = {"text": [], "timeout": 0.1}
    adapter.cfg = cfg
    page = StubPage(present={"input[placeholder*='请输入标题']", ".ql-editor",
                             "button:has-text('发布')"},
                    submits={"button:has-text('发布')"},
                    url="https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle")
    with pytest.raises(PublishError) as ei:
        adapter.publish(page, html="<p>x</p>", title="t")
    assert "未确认" in ei.value.message


def test_extra_steps_select_and_fill():
    adapter = get_adapter("wuba")
    adapter.cfg = {**adapter.cfg, "extra_steps": [
        {"select": "#cat", "value": "家政"}, {"fill": "#phone", "value": "13800000000"}]}
    page = StubPage(present={"input[placeholder*='标题']", "textarea",
                             "button:has-text('发布')"},
                    submits={"button:has-text('发布')"},
                    texts={"text=发布成功": "发布成功"})
    adapter.publish(page, html="<p>x</p>", title="t")
    assert ("#cat", "家政") in page.selects and ("#phone", "13800000000") in page.fills


# ── 工具函数 ───────────────────────────────────────────────────────────────

def test_plain_text_strips_tags_keeps_paragraphs():
    text = plain_text("<p>第一段</p><p>第二段</p><script>x=1</script>")
    assert text == "第一段\n第二段"


def test_playwright_driver_requires_usable_cookie(app):
    """Cookie 解不出内容 → 明确报 login_expired（而不是拿空 Cookie 硬发）。"""
    from app.models import Account
    from app.services.crypto import encrypt_text
    from app.services.publish.platforms import PlaywrightDriver
    from app.tenancy import tenant_scope

    with app.app_context():
        from app.extensions import db

        with tenant_scope("t_alpha"):
            db.session.add(Account(id="mp-cookie", tenant_id="t_alpha", platform="meipian",
                                   name="空 Cookie", daily_limit=1, used_today=0,
                                   quota_date=__import__("datetime").date.today(),
                                   cookie_blob=encrypt_text("")))
            db.session.commit()
            row = db.session.get(Account, "mp-cookie")
            with pytest.raises(PublishError) as ei:
                PlaywrightDriver("meipian").publish(html="<p>x</p>", title="t", account=row)
            assert ei.value.code == "login_expired"
            assert "Cookie" in ei.value.message


def test_platform_title_prefers_article_title():
    class _Art:
        title = "杭州上门检测怎么收费？先要书面报价"

    assert platform_title("目录名", _Art()).startswith("杭州上门检测")
    assert platform_title("目录名", None) == "目录名"
    assert len(platform_title("目录名", type("A", (), {"title": "长" * 99})())) == 64
