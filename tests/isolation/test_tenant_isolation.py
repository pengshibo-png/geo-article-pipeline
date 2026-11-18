"""租户隔离契约测试 V1–V6（ADR §2.6）。

V5（PG RLS 冒烟）需要 PostgreSQL —— SQLite 不支持 RLS，按 ADR-005 在 PG 环境单独跑，
此处以 `skip` 明示，避免"以为测过了"。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.errors import Forbidden, NotFound, TenantViolation
from app.extensions import db as _db
from app.models import Article, TENANT_SCOPED_TABLES, Title
from app.platform.storage import checkpoint, tenant_path
from app.tenancy import TenantScopedRepository, admin_scope, current_tenant, tenant_scope

BLUEPRINTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "blueprints"
BARE_QUERY_PATTERNS = (
    r"db\.session\.query\(",
    r"session\.execute\(",
    r"\.query\.filter\(\s*\)",          # 空过滤
)


# ── V1 静态扫描：blueprints 下禁止裸查询 ─────────────────────────────────────
def test_v1_no_bare_query_in_blueprints():
    offenders: list[str] = []
    for path in BLUEPRINTS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in BARE_QUERY_PATTERNS:
            if re.search(pattern, text):
                offenders.append(f"{path.relative_to(BLUEPRINTS_DIR.parent)} :: /{pattern}/")
    assert not offenders, f"V1 未通过，blueprints 下出现裸查询：{offenders}"


# ── V2 元数据自检：带 tenant_id 的表必须登记 ──────────────────────────────────
def test_v2_every_tenant_table_is_registered(app):
    with app.app_context():
        with_tenant = {
            table.name for table in _db.metadata.tables.values()
            if "tenant_id" in table.columns
        }
    unregistered = sorted(with_tenant - set(TENANT_SCOPED_TABLES))
    assert not unregistered, (
        f"V2 未通过：这些表有 tenant_id 却未登记进 TENANT_SCOPED_TABLES：{unregistered}"
    )


def test_v2_registry_has_no_phantom_tables(app):
    with app.app_context():
        existing = set(_db.metadata.tables.keys())
    phantom = sorted(set(TENANT_SCOPED_TABLES) - existing)
    assert not phantom, f"V2 未通过：登记册里有不存在的表：{phantom}"


# ── V3 before_flush：跨租户写入必须抛错 ──────────────────────────────────────
def test_v3_cross_tenant_insert_raises(app):
    with app.app_context():
        with tenant_scope("t_alpha"):
            with pytest.raises(TenantViolation):
                _db.session.add(Article(tenant_id="t_beta", dir="x", title="x"))
                _db.session.flush()
            _db.session.rollback()


def test_v3_tenant_id_auto_stamped(app):
    with app.app_context():
        with tenant_scope("t_alpha"):
            art = Article(dir="auto-stamp", title="自动补租户")
            _db.session.add(art)
            _db.session.flush()
            assert art.tenant_id == "t_alpha"
            _db.session.rollback()


# ── V4 跨租户读取 → 404（不是 403）──────────────────────────────────────────
def test_v4_cross_tenant_read_returns_404(app):
    with app.app_context():
        # 显式写入两个租户的数据（无上下文，直接指定 tenant_id）
        _db.session.add_all([
            Article(tenant_id="t_alpha", dir="alpha-dir", title="A 的文章"),
            Article(tenant_id="t_beta", dir="beta-dir", title="B 的文章"),
        ])
        _db.session.commit()

        with tenant_scope("t_alpha"):
            repo = TenantScopedRepository(Article)
            assert repo.get_or_404(dir="alpha-dir").title == "A 的文章"
            with pytest.raises(NotFound):          # ← 404 而非 403
                repo.get_or_404(dir="beta-dir")


# ── V6 连接/会话复用不串租户 ────────────────────────────────────────────────
def test_v6_scope_switch_does_not_leak(app):
    with app.app_context():
        _db.session.add_all([
            Article(tenant_id="t_alpha", dir="a1", title="A1"),
            Article(tenant_id="t_beta", dir="b1", title="B1"),
        ])
        _db.session.commit()
        repo = TenantScopedRepository(Article)

        with tenant_scope("t_alpha"):
            assert current_tenant() == "t_alpha"
            dirs_a = {a.dir for a in repo.all()}
        with tenant_scope("t_beta"):
            assert current_tenant() == "t_beta"
            dirs_b = {a.dir for a in repo.all()}

        assert "a1" in dirs_a and "b1" not in dirs_a
        assert "b1" in dirs_b and "a1" not in dirs_b


def test_scope_is_isolated_after_exit(app):
    with app.app_context():
        with tenant_scope("t_alpha"):
            pass
        from app.errors import AppError

        with pytest.raises(AppError):              # 退出后无上下文
            current_tenant()


# ── admin 跨租户：业务域只读（A18）──────────────────────────────────────────
def test_admin_cross_cannot_write_business_domain(app):
    with app.app_context():
        with admin_scope("t_beta", reason="客服排查"):
            with pytest.raises(Forbidden):
                _db.session.add(Article(dir="admin-write", title="admin 不该能写"))
                _db.session.flush()
            _db.session.rollback()


def test_admin_cross_requires_reason(app):
    with pytest.raises(Forbidden):
        with admin_scope("t_beta", reason=""):
            pass


# ── D4 路径租户化：防 `../` 穿越 ────────────────────────────────────────────
def test_tenant_path_blocks_escape(app):
    with app.app_context():
        app.config["DATA_ROOT"] = app.config["DATA_ROOT"]
        with tenant_scope("t_alpha"):
            ok = tenant_path("articles", "x", base=app.config["DATA_ROOT"])
            assert "t_alpha" in str(ok)
            with pytest.raises(Forbidden):
                tenant_path("..", "..", "t_beta", "secret.txt", base=app.config["DATA_ROOT"])


def test_checkpoint_reports_stage_state(app, tmp_path, monkeypatch):
    monkeypatch.setenv("GEO_ENV", "test")
    with app.app_context():
        with tenant_scope("t_alpha"):
            state = checkpoint("还不存在的目录", base=app.config["DATA_ROOT"])
            assert state == {"01_draft.html": False, "02_review.json": False, "03_final.html": False}


# ── V5 RLS：必须在 PostgreSQL 上跑 ──────────────────────────────────────────
@pytest.mark.skip(reason="V5 需要 PostgreSQL（SQLite 无 RLS）—— 按 ADR-005 在 PG 环境执行")
def test_v5_rls_smoke_pg_only():
    ...
