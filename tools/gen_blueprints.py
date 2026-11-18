# -*- coding: utf-8 -*-
"""脚手架：生成 6+1 蓝图骨架（Phase 0 交付物 2：目录 + 空 views / api / service）。

用法：./.venv/Scripts/python.exe tools/gen_blueprints.py
幂等：已存在的文件会被覆盖（脚手架，不承载业务逻辑）。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
BP_DIR = ROOT / "app" / "blueprints"

# name: (职责, 明确不做, [(视图函数, 路由, 模板)])
BP: dict[str, tuple[str, str, list[tuple[str, str, str]]]] = {
    "auth": ("注册/登录/登出/会话、租户开通、admin 跨租户视角", "不碰内容数据", [
        ("login_page", "/login", "login.html"),
        ("register_page", "/register", "register.html"),
        ("tenants_page", "/tenants", "tenants.html"),
        ("settings_page", "/settings", "settings.html"),
    ]),
    "console": ("首页聚合 4 个域的只读摘要", "不产生任何写操作", [
        ("home", "/", "client_home.html"),
    ]),
    "config": ("品牌资料/城市/画像/指令/素材（业务事实源）", "不触发生成", [
        ("brand_page", "/brand", "client_brand.html"),
        ("cities_page", "/cities", "client_cities.html"),
        ("audience_page", "/audience", "client_audience.html"),
        ("prompts_page", "/prompts", "client_prompts.html"),
        ("assets_page", "/assets", "client_assets.html"),
    ]),
    "topic": ("选题库 CRUD、AI 选题、同行抓取、文体配比", "不写正文", [
        ("titles_page", "/titles", "client_titles.html"),
    ]),
    "generation": ("A/B/C 流水线、抖音图文、视频产线", "不直接发布", [
        ("gen_page", "/gen", "client_gen.html"),
        ("douyin_page", "/douyin", "client_douyin.html"),
        ("video_page", "/video", "video.html"),
    ]),
    "distribution": ("账号授权、渠道体检、排队发布、发布日志、定时计划", "不生成内容", [
        ("tasks_page", "/tasks", "client_tasks.html"),
        ("archives_page", "/archives", "client_articles.html"),
        ("publog_page", "/publog", "client_publog.html"),
        ("myaccounts_page", "/auth", "client_myaccounts.html"),
    ]),
    "insight": ("GEO 采样、引用归因、报告生成与导出、竞品对比", "不改内容", [
        ("geo_page", "/geo", "client_geo.html"),
        ("citations_page", "/citations", "client_citations.html"),
        ("reports_page", "/reports", "reports.html"),
        ("competitor_page", "/competitor", "competitor.html"),
    ]),
}

INIT_T = '''"""{name} 蓝图 —— {duty}。

**明确不做**：{notdo}
"""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("{name}", __name__)

from app.blueprints.{name} import api, views  # noqa: E402,F401  注册路由
'''

VIEWS_HEAD = '''"""{name} 蓝图 · 页面路由（只解析/渲染，不写业务规则、不直接查库）。"""
from __future__ import annotations

from flask import render_template

from app.blueprints.{name} import bp
'''

VIEWS_PAGE = '''

@bp.get("{route}", endpoint="{func}")
def {func}():
    return render_template("_stub.html", page="{page}", route="{route}", blueprint="{name}")
'''

API_T = '''"""{name} 蓝图 · JSON 接口（同 views 的职责，返回 JSON）。"""
from __future__ import annotations

from flask import jsonify

from app.blueprints.{name} import bp


@bp.get("/api/{name}/ping", endpoint="{name}_ping")
def {name}_ping():
    return jsonify({{"ok": True, "blueprint": "{name}"}})
'''

SERVICE_T = '''"""{name} 蓝图 · 领域服务（业务规则/编排；不 import Flask request，不写裸 SQL）。

Phase 0 为骨架，Phase 2+ 逐个填充。所有数据访问必须经 TenantScopedRepository。
"""
from __future__ import annotations

from app.logging import get_logger

log = get_logger(__name__)


class {cls}Service:
    """{duty}"""

    def __init__(self, session=None) -> None:
        self.session = session
'''


def main() -> None:
    BP_DIR.mkdir(parents=True, exist_ok=True)
    (BP_DIR / "__init__.py").write_text(
        '"""蓝图包。所有蓝图在 create_app 中统一注册（见 app/blueprints/registry.py）。"""\n',
        encoding="utf-8",
    )
    for name, (duty, notdo, pages) in BP.items():
        d = BP_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text(INIT_T.format(name=name, duty=duty, notdo=notdo), encoding="utf-8")
        views = VIEWS_HEAD.format(name=name)
        views += "".join(
            VIEWS_PAGE.format(func=f, route=r, page=p, name=name) for f, r, p in pages
        )
        (d / "views.py").write_text(views, encoding="utf-8")
        (d / "api.py").write_text(API_T.format(name=name), encoding="utf-8")
        (d / "service.py").write_text(
            SERVICE_T.format(name=name, cls=name.capitalize(), duty=duty), encoding="utf-8"
        )
        print(f"  {name:13s} views={len(pages)}  api=1  service=1")

    registry = ["\"\"\"蓝图注册表：集中列出，供 create_app 遍历注册。\"\"\"",
                "from __future__ import annotations", "",
                "from app.blueprints.auth import bp as auth_bp",
                "from app.blueprints.config import bp as config_bp",
                "from app.blueprints.console import bp as console_bp",
                "from app.blueprints.distribution import bp as distribution_bp",
                "from app.blueprints.generation import bp as generation_bp",
                "from app.blueprints.insight import bp as insight_bp",
                "from app.blueprints.topic import bp as topic_bp", "",
                "BLUEPRINTS = (auth_bp, console_bp, config_bp, topic_bp, generation_bp,",
                "              distribution_bp, insight_bp)", ""]
    (BP_DIR / "registry.py").write_text("\n".join(registry), encoding="utf-8")
    print("蓝图骨架 + registry 生成完成")


if __name__ == "__main__":
    main()
