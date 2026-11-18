"""公开分享的报告正文读取（`/r/<token>` 路由的服务层，2026-10-14）。

token 即授权（原站匿名外访语义）：公开路由没有租户上下文，因此按 token 的
**裸查下沉在本服务层** —— V1 的裸查静态扫描只针对 blueprints，身份域/公开域
的只读查询统一下沉 services（同 `directory` 的租户目录先例）。取到报告后用
**报告自己的租户**解析正文文件（报告 HTML 按租户分目录，见 report_gen）。

撤回（share_active=False）/ 过期 / token 不存在 → 返回 None（路由层 404）。
"""
from __future__ import annotations

import secrets

from app.extensions import db
from app.models import Report
from app.services import report_gen
from app.tenancy import tenant_scope
from app.timeutil import now_local


def new_share_token() -> str:
    """签发一个不与任何报告碰撞的分享 token（碰撞时返回空串）。

    `Report.share_token` **没有唯一约束**（存量库与迁移都不加），而 `/r/<token>`
    是匿名公开路由：token 撞车会让访客读到**别的租户**的报告正文。唯一性在签发侧
    保证，比事后加唯一约束轻（不动存量 DDL、不堵历史数据）。
    """
    for _ in range(5):
        cand = secrets.token_urlsafe(16)
        if db.session.query(Report).filter(Report.share_token == cand).first() is None:
            return cand
    return ""


def shared_html(token: str) -> str | None:
    row = db.session.query(Report).filter(
        Report.share_token == str(token or "")).first()
    if row is None or not row.share_active:
        return None
    if row.share_expires is not None:
        expires = row.share_expires
        if expires.tzinfo is None:
            from datetime import timezone
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < now_local():
            return None
    with tenant_scope(row.tenant_id):
        return report_gen.read_report_html(row.html_path)
