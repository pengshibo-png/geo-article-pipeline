"""应用工厂（ADR-001：Flask + 应用工厂 + 蓝图，不迁 FastAPI）。

本函数是**装配**，不是业务：只负责按顺序接上扩展、蓝图、钩子。
业务逻辑一律在 `app/services/` 与 `app/blueprints/*/service.py`。
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request

from app.config import load_config
from app.errors import register_error_handlers
from app.extensions import db
from app.logging import bind_request_id, configure_logging

__all__ = ["create_app"]


def _install_request_id(app: Flask) -> None:
    @app.before_request
    def _bind_request_id():                     # pragma: no cover - 集成测试覆盖
        bind_request_id(request.headers.get("X-Request-Id"))


def _register_core_routes(app: Flask, env: str) -> None:
    @app.get("/healthz", endpoint="console.healthz")
    def healthz():                              # 公开端点（无需租户上下文）
        return jsonify({"ok": True, "env": env, "service": "geo-article-pipeline"})


#: 全站安全响应头。**故意不写 `script-src`**：页面里 67 个 `<script>` 块与 389 个
#: 行内 `onclick=` 全靠内联脚本，任何 `script-src`（哪怕加 `unsafe-inline`）
#: 要么打断整个界面、要么等于没写 —— 所以脚本层面的防线放在
#: **写侧消毒**（writer/html_sanitize.py，模型产出 HTML 的白名单重建）
#: 与**读侧沙箱**（档案页预览走 `sandbox=""` 的 iframe）上，不在 CSP。
#: 同理不加 `frame-ancestors` / `X-Frame-Options`：`/r/<token>` 是给客户分享用的
#: 公开页，将来若被嵌到客户自己的页面里，禁框会静默弄坏一个对外功能。
#: 这里保留的三条对任何页面都无副作用，且堵掉各自独立的注入放大器：
#:   object-src    —— 插件内容（Flash/PDF 嵌入）执行
#:   base-uri      —— 注入 `<base href>` 改掉页面内所有相对地址的解析基准
#:   form-action   —— 注入的表单把数据 POST 到外部域
_SECURITY_HEADERS = {
    "Content-Security-Policy": "object-src 'none'; base-uri 'self'; form-action 'self'",
    # 上传的素材由同源 /media/<path> 直接吐字节：没有 nosniff，浏览器可能把
    # 一个内容是 HTML 的"图片"按 text/html 嗅探执行（上传即得 XSS）。
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def _install_security_headers(app: Flask) -> None:
    @app.after_request
    def _security_headers(resp):                # pragma: no cover - 集成测试覆盖
        for name, value in _SECURITY_HEADERS.items():
            resp.headers.setdefault(name, value)
        return resp


def create_app(env: str | None = None) -> Flask:
    settings = load_config(env)

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SQLALCHEMY_DATABASE_URI=settings.database_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        DATA_ROOT=str(settings.data_root),
        GEO_SETTINGS=settings,
        GEN_POOL_SIZE=settings.gen_pool_size,
        JSON_AS_ASCII=False,
        CSRF_ENABLED=settings.csrf_enabled,
    )

    configure_logging(settings.log_level, settings.log_json)
    db.init_app(app)

    from app import models  # noqa: F401  导入以注册全部模型元数据

    register_error_handlers(app)
    _install_request_id(app)
    _install_security_headers(app)
    _register_core_routes(app, settings.env)

    from app.webassets import install_asset_version
    install_asset_version(app)

    from app.blueprints.registry import BLUEPRINTS
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    from app.csrf import install_csrf
    install_csrf(app)                  # CSRF：非安全方法校验会话令牌（可 GEO_CSRF_ENABLED=0 关）

    from app.tenancy import init_tenancy, install_rls_tenant_hook
    init_tenancy(app)
    with app.app_context():
        from app.sqlite_tuning import install_sqlite_pragmas
        install_sqlite_pragmas(db.engine)    # SQLite：WAL + busy_timeout（并发写不再 locked）
        install_rls_tenant_hook(db.engine)   # PG 行级安全的事务级上下文（SQLite 自动跳过）

    from app.platform.jobs import init_jobs
    init_jobs(app)

    from app.services.writer import review  # noqa: F401  导入以注册流水线任务（review 级联导入 pipeline）
    from app.services.writer import managed  # noqa: F401  导入以注册页面驱动的受管任务（状态桥）
    from app.services.publish import executor  # noqa: F401  导入以注册 publish.run 任务
    from app.services import geo_monitor  # noqa: F401  导入以注册 geo.run 采样任务
    from app.services import report_gen  # noqa: F401  导入以注册 report.generate 任务
    from app.blueprints.generation import douyin_jobs  # noqa: F401  导入以注册 douyin.generate/publish 任务
    from app.blueprints.generation import douyin_poi_jobs  # noqa: F401  注册 douyin.poipool.build
    from app.blueprints.generation import video_jobs  # noqa: F401  导入以注册 video.generate/publish 任务
    from app.blueprints.config import audience_jobs  # noqa: F401  导入以注册 audience.analyze 任务
    from app.blueprints.topic import scrape_jobs  # noqa: F401  导入以注册 titles.scrape 任务

    from app.dbpatch import patch_sqlite_columns
    patch_sqlite_columns(app)

    Path(app.config["DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
    return app
