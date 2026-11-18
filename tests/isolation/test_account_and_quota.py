"""ADR 验收项 A14/A16/A20/A21/A22/A23 的直接证据。

这些都属于 Phase 0 的 DDL 修订范围（账号单表 / 额度原子扣减 / 索引策略）。
> A14/A23 的"真并发"版本（多线程）需要 PostgreSQL，SQLite 写锁会让结果不稳定；
> 这里用**顺序执行 100 次原子 UPDATE**验证 SQL 语义，并发版本列入 PG 阶段。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.blueprints.distribution.service import DistributionService
from app.extensions import db as _db
from app.models import Account
from app.tenancy import TenantScopedRepository, tenant_scope

DAY1, DAY2 = date(2026, 10, 10), date(2026, 10, 11)


def _add_account(acc_id: str, tenant: str, *, limit: int = 50, used: int = 0,
                 day: date = DAY1, platform: str = "meipian", name: str = "美篇主号") -> None:
    _db.session.add(Account(
        id=acc_id, tenant_id=tenant, platform=platform, name=name,
        daily_limit=limit, used_today=used, quota_date=day,
    ))
    _db.session.commit()


def _atomic_decrement(acc_id: str, tenant: str, day: date) -> int:
    result = _db.session.execute(
        text(Account.quota_update_sql()),
        {"id": acc_id, "tenant_id": tenant, "today": day},
    )
    _db.session.commit()
    return result.rowcount


# ── A16 每张租户表都有 tenant_id 前置索引（或 tenant_id 在主键里）────────────
def test_a16_tenant_leading_index_on_every_tenant_table(app):
    with app.app_context():
        missing = []
        for name, table in _db.metadata.tables.items():
            if "tenant_id" not in table.columns:
                continue
            pk_cols = [c.name for c in table.primary_key.columns]
            tenant_first_index = any(
                list(idx.columns)[0].name == "tenant_id"
                for idx in table.indexes if list(idx.columns)
            )
            if "tenant_id" not in pk_cols and not tenant_first_index:
                missing.append(name)
    assert not missing, f"A16 未通过：以下租户表缺 tenant_id 前置索引：{missing}"


# ── A20 第二个租户绑定同一物理账号 → 主键冲突（无需应用逻辑）────────────────
def test_a20_second_tenant_cannot_bind_same_account(app):
    with app.app_context():
        _add_account("mp-1788448480", "t_alpha")
        _db.session.add(Account(
            id="mp-1788448480", tenant_id="t_beta", platform="meipian", name="同一个物理账号",
            daily_limit=10, used_today=0, quota_date=DAY1,
        ))
        with pytest.raises(IntegrityError):
            _db.session.commit()
        _db.session.rollback()


# ── A23 / A14 额度原子扣减：100 次扣减 / 额度 50 → 恰好 50 次成功 ────────────
def test_a23_quota_atomic_decrement_no_overrun(app):
    with app.app_context():
        _add_account("acc-quota", "t_alpha", limit=50)
        succeeded = sum(_atomic_decrement("acc-quota", "t_alpha", DAY1) for _ in range(100))
        row = _db.session.get(Account, "acc-quota")
    assert succeeded == 50, f"A23 未通过：成功 {succeeded} 次（应为 50，无超发）"
    assert row.used_today == 50


def test_a23_daily_limit_zero_means_registration_only(app):
    """daily_limit = 0 表示登记型账号，不参与发布 —— 原子扣减应始终成功。"""
    with app.app_context():
        _add_account("acc-reg", "t_alpha", limit=0)
        succeeded = sum(_atomic_decrement("acc-reg", "t_alpha", DAY1) for _ in range(5))
    assert succeeded == 5, "limit=0 的登记型账号不应被额度拦住（由发布层排除）"


# ── A22 自然日额度翻转（Asia/Shanghai 的自然日，ADR-013）────────────────────
def test_a22_quota_resets_on_next_natural_day(app):
    with app.app_context():
        _add_account("acc-day", "t_alpha", limit=50, used=50, day=DAY1)
        assert _atomic_decrement("acc-day", "t_alpha", DAY1) == 0        # 当天已满

        assert _atomic_decrement("acc-day", "t_alpha", DAY2) == 1        # 次日自动重置
        row = _db.session.get(Account, "acc-day")
    assert row.used_today == 1
    assert row.quota_date == DAY2


# ── A21 虚拟聚合渠道：响应里有，库里没有 ────────────────────────────────────
def test_a21_virtual_aggregate_channels_are_never_persisted(app):
    with app.app_context():
        _add_account("mp-1", "t_alpha", platform="meipian", name="美篇主号")
        _add_account("mp-2", "t_alpha", platform="meipian", name="美篇副号")
        _add_account("sh-1", "t_alpha", platform="souhu", name="搜狐号")

        with tenant_scope("t_alpha"):
            accounts = TenantScopedRepository(Account).all()
        channels = DistributionService.build_channels(accounts)
        channel_ids = {c["id"] for c in channels}

        stored_ids = {a.id for a in _db.session.query(Account).all()}

    assert {"mp-all", "tenant-all"} <= channel_ids, "聚合渠道应出现在响应里"
    assert "mp-all" not in stored_ids and "tenant-all" not in stored_ids, \
        "A21 未通过：虚拟渠道被写进了 account 表"
    assert all(c["virtual"] for c in channels if c["id"].endswith("-all"))


def test_a21_virtual_channel_cannot_be_used_for_publishing(app):
    """发布时必须解析到真实账号；虚拟聚合项不可发布（否则会"选到虚拟账号"）。"""
    with app.app_context():
        _add_account("mp-1", "t_alpha", platform="meipian", name="美篇主号")
        with tenant_scope("t_alpha"):
            accounts = TenantScopedRepository(Account).all()
        assert DistributionService.resolve_real_account("mp-all", accounts) is None
        assert DistributionService.resolve_real_account("tenant-all", accounts) is None
        assert DistributionService.resolve_real_account("mp-1", accounts) is not None
