"""前端还原门禁（B1 起）：静态资产在位、版本注入、layout.js 适配点。

页面级门禁（22 路由渲染/CSS 顺序/数据清洗）随 B2-B6 各批补齐。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "static"
TEMPLATES = ROOT / "app" / "templates"

# 按参考系统实测逐字节还原的 7 个真实资产（md5 校验过），404 存根一律不收
REAL_ASSETS = (
    "client_style.css", "ds.css", "theme-light.css", "style.css",
    "icons.js", "layout.js", "register.js",
)


def test_static_assets_on_disk():
    for name in REAL_ASSETS:
        p = STATIC / name
        assert p.is_file(), f"缺静态资产: {name}"
        assert p.stat().st_size > 200, f"疑似 404 存根(207B): {name}"


def test_static_served_over_http(client):
    for name in REAL_ASSETS:
        r = client.get(f"/static/{name}")
        assert r.status_code == 200, f"/static/{name} -> {r.status_code}"


def test_layout_js_adapted():
    """layout.js 两处适配：登出端点、view-tenant 入参（原站形状 → 工程契约）。"""
    text = (STATIC / "layout.js").read_text(encoding="utf-8")
    assert "/api/auth/logout" in text
    assert "'/api/logout'" not in text
    assert "tenant_id: tid" in text


def test_stub_kept_for_desktop_build():
    """_stub.html 必须保留——build/build_desktop.py 的产物校验依赖它。"""
    assert (TEMPLATES / "_stub.html").is_file()


def test_asset_version_injected(app):
    with app.app_context():
        from flask import render_template_string
        assert render_template_string("{{ asset_ver }}") == "20261012a"


# ---- B2 登录/注册 -----------------------------------------------------------

def test_public_pages_render(client):
    """公开页 200、非占位、关键 JS 挂载点在位。"""
    for path, marker in (("/login", "doLogin"), ("/register", "REG.open")):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        html = r.get_data(as_text=True)
        assert "_stub" not in html, f"{path} 仍是占位页"
        assert marker in html, f"{path} 缺 {marker}"


def test_login_page_contract():
    text = (TEMPLATES / "login.html").read_text(encoding="utf-8")
    # CSS 顺序硬要求(ds→theme-light)，且登录页不引甲方壳
    assert text.index("ds.css") < text.index("theme-light.css")
    assert "client_style.css" not in text
    assert '<html lang="zh-CN" class="theme-light">' in text
    # 登录提交走工程契约端点与入参
    assert "/api/auth/login" in text
    assert "username: id" in text and "password: pass" in text


# ---- B3 config 五页 ---------------------------------------------------------

B3_PAGES = (
    ("/brand", "client_brand.html", "loadBrand"),
    ("/cities", "client_cities.html", "batchAdd"),
    ("/audience", "client_audience.html", "saveProfile"),
    ("/prompts", "client_prompts.html", "saveGenreMix"),
    ("/assets", "client_assets.html", "imgFallback"),
)


def test_b3_pages_render(client):
    """登录后 200、非占位、关键 JS 挂载函数在位、theme-light 壳。"""
    from tests.conftest import login
    login(client, "alpha", "pw-alpha")
    for path, tpl, marker in B3_PAGES:
        text = (TEMPLATES / tpl).read_text(encoding="utf-8")
        assert '<html lang="zh-CN" class="theme-light">' in text, tpl
        assert "?v={{ asset_ver }}" in text, tpl
        assert "?v=2026" not in text, f"{tpl} 有硬编码版本号"
        assert marker in text, f"{tpl} 缺 {marker}"
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        html = r.get_data(as_text=True)
        assert "_stub" not in html, f"{path} 仍是占位页"
        assert marker in html, f"{path} 缺 {marker}"


def test_b3_css_order():
    """CSS 加载顺序硬要求：cities 领 style.css(管理壳)；其余 client_style 打头。"""
    cities = (TEMPLATES / "client_cities.html").read_text(encoding="utf-8")
    assert cities.index("style.css") < cities.index("client_style.css")
    assert cities.index("client_style.css") < cities.index("ds.css")
    assert cities.index("ds.css") < cities.index("theme-light.css")
    for _, tpl, _ in B3_PAGES:
        if tpl == "client_cities.html":
            continue
        text = (TEMPLATES / tpl).read_text(encoding="utf-8")
        assert text.index("client_style.css") < text.index("ds.css"), tpl
        assert text.index("ds.css") < text.index("theme-light.css"), tpl
        assert "style.css?" not in text.replace("client_style.css?", ""), f"{tpl} 不应引管理壳 style.css"


def test_b3_no_tenant_data_leak():
    """对照页携带的原站租户样本不得进入模板。"""
    import re
    for _, tpl, _ in B3_PAGES:
        text = (TEMPLATES / tpl).read_text(encoding="utf-8")
        assert not re.search(r"juxiange|聚贤阁|meipian\.cn|a7x2k9m4", text), tpl


# ---- B4 核心流程三页 --------------------------------------------------------

B4_PAGES = (
    ("/titles", "client_titles.html", "scrapePoll"),
    ("/gen", "client_gen.html", "pauseGen"),
    ("/archives", "client_archives.html", "chanBadges"),
)


def test_b4_pages_render(client):
    """登录后 200、非占位、关键 JS 函数在位、版本 token 注入。"""
    from tests.conftest import login
    login(client, "alpha", "pw-alpha")
    for path, tpl, marker in B4_PAGES:
        text = (TEMPLATES / tpl).read_text(encoding="utf-8")
        assert '<html lang="zh-CN" class="theme-light">' in text, tpl
        assert "?v={{ asset_ver }}" in text, tpl
        assert "?v=2026" not in text, f"{tpl} 有硬编码版本号"
        assert marker in text, f"{tpl} 缺 {marker}"
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        html = r.get_data(as_text=True)
        assert "_stub" not in html, f"{path} 仍是占位页"
        assert marker in html, f"{path} 缺 {marker}"


def test_b4_css_order():
    """三页均为 client_style→ds→theme-light，不带管理壳 style.css。"""
    for _, tpl, _ in B4_PAGES:
        text = (TEMPLATES / tpl).read_text(encoding="utf-8")
        assert text.index("client_style.css") < text.index("ds.css"), tpl
        assert text.index("ds.css") < text.index("theme-light.css"), tpl
        assert "style.css?" not in text.replace("client_style.css?", ""), tpl


def test_b4_no_tenant_data_leak():
    import re
    for _, tpl, _ in B4_PAGES:
        text = (TEMPLATES / tpl).read_text(encoding="utf-8")
        assert not re.search(r"juxiange|聚贤阁|meipian\.cn|a7x2k9m4", text), tpl


# ---- B5 首页（效果总览）------------------------------------------------------

def test_b5_home_page(client):
    from tests.conftest import login
    login(client, "alpha", "pw-alpha")
    text = (TEMPLATES / "client_home.html").read_text(encoding="utf-8")
    assert "vendor/echarts.min.js" not in text          # 未抓到的 vendor 不许引
    assert "?v=2026" not in text
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "_stub" not in html and "renderEngineChart" in html


# ---- B6 其余 11 页（9 复制 + tenants/settings 自建）--------------------------

B6_PAGES = (
    ("/douyin", "client_douyin.html", "douyin"),
    ("/video", "client_video.html", "video"),
    ("/tasks", "client_tasks.html", "tasks"),
    ("/publog", "client_publog.html", "publog"),
    ("/auth", "client_myaccounts.html", "auth"),
    ("/geo", "client_geo.html", "geo"),
    ("/citations", "client_citations.html", "citations"),
    ("/reports", "client_reports.html", "reports"),
    ("/competitor", "client_competitor.html", "competitor"),
    ("/tenants", "client_tenants.html", "tenants"),
    ("/settings", "client_settings.html", "settings"),
)


def test_b6_pages_render(client):
    """22 路由还原收口：B6 全部登录后 200、非占位、data-page 与 nav id 一致。"""
    from tests.conftest import login
    login(client, "alpha", "pw-alpha")
    for path, tpl, nav_id in B6_PAGES:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        html = r.get_data(as_text=True)
        assert "_stub" not in html, f"{path} 仍是占位页"
        assert f'data-page="{nav_id}"' in html, f"{path} nav id 不符"


def test_b6_no_vendor_or_leak():
    import re
    for _, tpl, _ in B6_PAGES:
        text = (TEMPLATES / tpl).read_text(encoding="utf-8")
        assert "?v={{ asset_ver }}" in text, tpl        # 版本注入到位
        assert not re.search(r"\?v=2026\d", text), tpl  # 无硬编码版本残留
        assert "vendor/echarts" not in text, tpl        # echarts 走 typeof 守卫
        assert not re.search(r"juxiange|聚贤阁|meipian\.cn|a7x2k9m4|江屿", text), tpl


def test_b6_admin_pages(client):
    """tenants/settings 对 admin 与 client 都是 200（权限差异在页面内容，不在路由）。"""
    from tests.conftest import login
    login(client, "root", "pw-root")
    for path, _, _ in (("/tenants", "client_tenants.html", "tenants"),
                       ("/settings", "client_settings.html", "settings")):
        assert client.get(path).status_code == 200, path


# ---- 内联 JS 语法门禁 --------------------------------------------------------

JS_PAGES = (
    "/", "/brand", "/cities", "/audience", "/prompts", "/assets", "/titles",
    "/gen", "/archives", "/douyin", "/video", "/tasks", "/publog", "/auth",
    "/geo", "/citations", "/reports", "/competitor", "/tenants", "/settings",
)


def _inline_scripts(html: str) -> list[str]:
    """取页面内联 <script>（排除带 src 的外链）。"""
    return [b for b in re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I) if b.strip()]


@pytest.mark.skipif(shutil.which("node") is None, reason="未安装 node，跳过 JS 语法校验")
def test_inline_js_parses(client):
    """每个页面的内联 <script> 必须通过 node 语法检查。

    为什么需要（2026-10-16 踩过）：模板把整页逻辑放在**一个** <script> 块里，
    块内任何一处语法错误都会让整块不执行 —— 表现是「页面列表空白、什么都不显示」，
    而不是控制台可见的报错。当时给发布弹窗加空状态三元表达式漏了 `(chs.length ?`，
    结果文章档案整页失效、文章列表全空，看起来像"文章丢了"。
    """
    from tests.conftest import login
    login(client, "alpha", "pw-alpha")
    problems: list[str] = []
    for path in JS_PAGES:
        r = client.get(path)
        if r.status_code != 200:
            continue
        for i, block in enumerate(_inline_scripts(r.get_data(as_text=True))):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as fh:
                fh.write(block)
                tmp = fh.name
            proc = subprocess.run(["node", "--check", tmp],
                                  capture_output=True, text=True)
            Path(tmp).unlink(missing_ok=True)
            if proc.returncode != 0:
                last = (proc.stderr or "").strip().splitlines()[-1:]
                problems.append(f"{path} block#{i}: {last[0] if last else 'syntax error'}")
    assert not problems, "内联 JS 语法错误（会导致整页脚本不执行）：\n" + "\n".join(problems)
