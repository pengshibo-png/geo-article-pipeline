"""多租户隔离 ★ P0 正确性核心（ARCH-ADR-001 v1.1 §2 / §3）。

四道防线：
  D1 租户上下文  —— tenant_id **只来自 session**，永不来自参数/Header/Body
  D2 仓储强制注入 —— 调用方拿不到裸 session，查询自动加 tenant_id 过滤
  D3 PG RLS 兜底  —— 由 alembic 迁移建策略（D3 本身在 SQLAlchemy 之外）
  D4 约束与路径   —— 复合唯一键含 tenant_id；tenant_path() 防 `../` 穿越

设计原则：**不靠纪律，靠机制**。所以调用方"没有忘记加过滤的机会"。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from sqlalchemy import event
from sqlalchemy.orm import Session as SASession

from app.errors import AppError, Forbidden, NotFound, TenantViolation
from app.extensions import db
from app.logging import get_logger, tenant_id_var

log = get_logger(__name__)

_scope: ContextVar["TenantScope | None"] = ContextVar("tenant_scope", default=None)


class TenantContextMissing(AppError):
    """在有租户表的访问路径上却没有绑定租户 —— 属于内部错误（500）。"""

    code, http_status, message = "tenant_context_missing", 500, "租户上下文未绑定（内部错误）"


@dataclass(frozen=True)
class TenantScope:
    tenant_id: str
    is_admin_cross: bool = False     # admin view-tenant 时为 True（业务域只读）
    reason: str | None = None         # 跨租户必须给理由


# ── D1 上下文 ────────────────────────────────────────────────────────────────

def current_scope() -> TenantScope:
    scope = _scope.get()
    if scope is None:
        raise TenantContextMissing("当前执行上下文没有租户；请用 tenant_scope() 包裹")
    return scope


def current_tenant() -> str:
    return current_scope().tenant_id


def is_admin_cross() -> bool:
    scope = _scope.get()
    return bool(scope and scope.is_admin_cross)


@contextmanager
def tenant_scope(
    tenant_id: str, *, is_admin_cross: bool = False, reason: str | None = None
) -> Iterator[TenantScope]:
    scope = TenantScope(tenant_id, is_admin_cross, reason)
    token = _scope.set(scope)
    prev_log_tid = tenant_id_var.get()
    tenant_id_var.set(tenant_id)
    try:
        yield scope
    finally:
        _scope.reset(token)
        tenant_id_var.set(prev_log_tid)


def install_rls_tenant_hook(engine) -> None:
    """PostgreSQL 行级安全：每个事务开启时注入「事务级」租户上下文。

    迁移 c5300fa6dfa8 给 20 张表加了
    `tenant_id = current_setting('app.tenant_id', true)` 策略。应用侧不注入该
    上下文的话，`current_setting` 恒为 NULL → 切到 PG 后**所有租户查询被 RLS
    拦成 0 行**（比不做 RLS 更致命：看起来像库空了）。

    用 `set_config('app.tenant_id', tid, true)`（第三参 is_local=true 等价于
    `SET LOCAL`）：事务结束即自动失效，**不会**串到连接池里的下一个请求 ——
    会话级 `SET` 会串，这是迁移注释里点名的坑。

    绑 `begin` 事件（每个事务一次）而非 `engine_connect`（每次借连接一次）：
    保证语句一定落在事务内，且不受连接池复用影响。SQLite 方言直接跳过（无 RLS）。
    无租户上下文时跳过（例如迁移/DDL）—— 对 RLS 而言这是 fail-closed。
    """
    from sqlalchemy import event

    @event.listens_for(engine, "begin")
    def _receive_begin(conn):  # noqa: ANN001 - SQLAlchemy 事件签名
        if conn.dialect.name != "postgresql":
            return
        scope = _scope.get()
        tid = scope.tenant_id if scope else None
        if not tid:
            return
        conn.exec_driver_sql(
            "SELECT set_config('app.tenant_id', %s, true)", (str(tid),))


@contextmanager
def admin_scope(tenant_id: str, *, reason: str, actor: str = "-") -> Iterator[TenantScope]:
    """admin 跨租户的**唯一入口**（ADR §2.4）。必须传 reason，否则拒绝。

    注意：本上下文管理器**不写库**（避免在 CM 里做隐藏写入）。
    审计由调用方显式调用 `write_audit()` 落库 —— 见 `app/models/tenant.py` 的 AuditLog。
    """
    if not reason or not reason.strip():
        raise Forbidden("admin 跨租户访问必须提供 reason")
    with tenant_scope(tenant_id, is_admin_cross=True, reason=reason):
        log.warning(
            "admin_cross_access",
            extra={"event": "admin_cross_access", "actor": actor, "reason": reason},
        )
        yield TenantScope(tenant_id, is_admin_cross=True, reason=reason)


def write_audit(actor: str, tenant_id: str, reason: str | None, endpoint: str | None) -> None:
    """记录一次 admin 跨租户访问（ADR §3.3）。"""
    from datetime import datetime, timezone
    from app.models.tenant import AuditLog

    db.session.add(
        AuditLog(
            actor=actor,
            tenant_id=tenant_id,
            reason=reason,
            endpoint=endpoint,
            happened_at=datetime.now(timezone.utc),
        )
    )
    # 必须显式提交：否则 app context teardown 时 session.remove() 会把未提交的
    # 审计行一起回滚 —— "跨租户访问留痕"就静默失效了。
    db.session.commit()


# ── D2 仓储层强制注入 ────────────────────────────────────────────────────────

class TenantScopedRepository:
    """所有租户表访问的**唯一入口**。裸查被 CI 静态扫描禁止（V1）。"""

    def __init__(self, model: Any, session: Any | None = None) -> None:
        table = getattr(model, "__table__", None)
        if table is None or "tenant_id" not in table.columns:
            raise TypeError(f"{getattr(model, '__name__', model)} 不是租户表，不能用 scoped 仓储")
        self.model = model
        self.session = session or db.session

    def query(self):
        return self.session.query(self.model).filter(self.model.tenant_id == current_tenant())

    def all(self, **kw) -> list[Any]:
        return self.query().filter_by(**kw).all()

    def first(self, **kw) -> Any | None:
        return self.query().filter_by(**kw).first()

    def get_or_404(self, **kw) -> Any:
        # 跨租户 → 404（不是 403），避免存在性泄漏（V4）
        obj = self.first(**kw)
        if obj is None:
            raise NotFound(f"{self.model.__name__} 不存在")
        return obj

    def count(self, **kw) -> int:
        return self.query().filter_by(**kw).count()

    def add(self, obj: Any) -> Any:
        obj.tenant_id = current_tenant()      # 显式注入，不依赖事件
        self.session.add(obj)
        return obj

    def delete(self, obj: Any) -> None:
        if getattr(obj, "tenant_id", None) != current_tenant():
            raise TenantViolation("删除他租户对象被拒绝")
        self.session.delete(obj)


# ── D2 写侧兜底（before_flush）───────────────────────────────────────────────

def _table_name(obj: Any) -> str | None:
    table = getattr(obj, "__table__", None)
    return table.name if table is not None else None


def _has_tenant_col(obj: Any) -> bool:
    table = getattr(obj, "__table__", None)
    return table is not None and "tenant_id" in table.columns


@event.listens_for(SASession, "before_flush")
def _stamp_and_guard(session: SASession, flush_context: Any, instances: Any) -> None:
    from app.models import IDENTITY_TABLES  # 延迟导入，避免模型初始化顺序问题

    scope = _scope.get()
    if scope is None:
        return                                   # 迁移/脚本等无租户场景不干预
    tid = scope.tenant_id

    pending = list(session.new) + list(session.dirty) + list(session.deleted)
    for obj in pending:
        if not _has_tenant_col(obj):
            continue
        # Q3：admin 跨租户视角只能写身份域
        if scope.is_admin_cross and _table_name(obj) not in IDENTITY_TABLES:
            raise Forbidden(f"admin 跨租户视角禁止写业务数据（{_table_name(obj)}）")
        current = getattr(obj, "tenant_id", None)
        if obj in session.new and current is None:
            obj.tenant_id = tid                   # 自动补齐
        elif current != tid:
            raise TenantViolation(
                f"跨租户写入被拒绝：对象 tenant_id={current!r}，当前租户={tid!r}"
            )


# ── D1 请求钩子 ──────────────────────────────────────────────────────────────

PUBLIC_ENDPOINTS = frozenset(
    {"auth.login_page", "auth.login_api", "auth.register_page", "auth.register_api",
     "console.healthz", "static", "auth.logout_api",
     "insight.report_share_page",       # /r/<token> 公开分享（token 即授权，原站语义）
     "console.csrf_token_api"}          # /api/csrf：取会话令牌（登录前也要能拿）
)


def init_tenancy(app) -> None:
    """注册请求级租户绑定（ADR §2.3 D1）。"""
    from flask import request, session

    from app.errors import Unauthorized

    @app.before_request
    def _bind_tenant():                       # pragma: no cover - 由集成测试覆盖
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("static"):
            return
        uid = session.get("uid")
        if not uid:
            raise Unauthorized()
        tid = session.get("view_tenant") or uid      # admin 切租户落在 session，不落 URL
        token = _scope.set(
            TenantScope(tid, is_admin_cross=(tid != uid), reason=session.get("view_reason"))
        )
        request.environ["_geo_scope_token"] = token
        tenant_id_var.set(tid)

    @app.teardown_request
    def _unbind_tenant(exc):                  # pragma: no cover
        token = request.environ.pop("_geo_scope_token", None)
        if token is not None:
            _scope.reset(token)
            tenant_id_var.set(None)
