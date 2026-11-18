"""模型包：集中导出 + 租户表登记册（V2 元数据自检的依据）。

`TENANT_SCOPED_TABLES` 是**显式登记册**：任何新增的、带 tenant_id 列的表，
如果没被登记在这里，`tests/isolation/test_v2_metadata.py` 会失败。
这就是 ADR §2.6 里 V2 要拦的"新模型忘了纳入隔离机制"。
"""
from __future__ import annotations

from app.models.account import Account
from app.models.article import Article, ArticleJob
from app.models.base import TenantScopedMixin, TimestampMixin
from app.models.config import Asset, Audience, Brand, City, Prompt, TitleConfig
from app.models.geo import GeoDaily, GeoResult
from app.models.publish import PublishLog, PublishTask, Schedule
from app.models.report import Report, ReportQuota
from app.models.tenant import AuditLog, Tenant
from app.models.title import CompetitorTitle, GenreMix, Title
from app.models.user import AppUser, LoginAttempt

# 身份域：admin 跨租户视角下**允许写**的表（ADR-003 §3.1 修订）
# audit_log 在内：admin 切换租户视角的审计动作本身就发生在跨租户上下文里，
# 若不放开，write_audit 会被 before_flush 拦成 Forbidden（留痕反而写不进去）。
IDENTITY_TABLES: frozenset[str] = frozenset(
    {"tenant", "app_user", "login_attempt", "audit_log"})

# 带 tenant_id 列的全部表（含身份域中带 tenant_id 的两张）
TENANT_SCOPED_TABLES: frozenset[str] = frozenset(
    {
        "app_user",
        "login_attempt",
        "audit_log",
        "brand",
        "city",
        "audience",
        "prompt",
        "asset",
        "title_config",
        "title",
        "genre_mix",
        "competitor_title",
        "article",
        "article_job",
        "account",
        "publish_task",
        "publish_log",
        "schedule",
        "geo_result",
        "geo_daily",
        "report",
        "report_quota",
    }
)

ALL_MODELS = (
    Tenant, AuditLog, AppUser, LoginAttempt,
    Brand, City, Audience, Prompt, Asset, TitleConfig,
    Title, GenreMix, CompetitorTitle,
    Article, ArticleJob,
    Account,
    PublishTask, PublishLog, Schedule,
    GeoResult, GeoDaily,
    Report, ReportQuota,
)

__all__ = [
    "Tenant", "AuditLog", "AppUser", "LoginAttempt",
    "Brand", "City", "Audience", "Prompt", "Asset", "TitleConfig",
    "Title", "GenreMix", "CompetitorTitle",
    "Article", "ArticleJob", "Account",
    "PublishTask", "PublishLog", "Schedule",
    "GeoResult", "GeoDaily", "Report", "ReportQuota",
    "TenantScopedMixin", "TimestampMixin",
    "IDENTITY_TABLES", "TENANT_SCOPED_TABLES", "ALL_MODELS",
]
