"""insight 蓝图 · 报告中心（reports.html 诊断报告契约）。

接口形状实测自 参考系统页面实测：
- GET  /api/report/list?page&size&status&days&q → {total, pages, rows, quota:{limit,used,total_cost}}
- POST /api/report/generate → {report_id}；拒绝=非200 {"error":中文文案}（页面原样展示）
- GET  /api/report/status?id → {status, step, fail_reason}
- GET  /api/report/html?id / /api/report/pdf?id → 报告正文（pdf=打印版体验）
- POST /api/report/share {id, days} → {token, expires_at}
- POST /api/report/share/revoke {token} → {ok}
- POST /api/report/retry {id, step} → {report_id}

流水线任务体在 `app/services/report_gen.py`（report.generate，queue="report"）。
分享链接 `/r/<token>` **匿名可访**（原站语义：token 即授权，无需登录）——
公开路由无租户上下文，故按 token 的裸查下沉在本服务层（同 directory 的身份域
先例：V1 扫描只针对 blueprints），取到报告后用**报告自己的租户**解析正文文件。
"""
from __future__ import annotations

from datetime import timedelta

from app.errors import NotFound
from app.extensions import db as _db
from app.models import Report, ReportQuota
from app.services import directory, report_gen, report_share
from app.tenancy import TenantScopedRepository, current_tenant
from app.timeutil import hm_string, now_local

_PAGE_STEPS = ("queued", "generating", "verifying", "rendering", "done", "failed")


class ReportService:
    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    # ── 列表 ────────────────────────────────────────────────────────────────

    def list_reports(self, *, page=1, size=20, status="", days=0, q="") -> dict:
        try:
            page = max(1, int(page)); size = max(1, min(int(size), 100)); days = max(0, int(days))
        except (TypeError, ValueError):
            page, size, days = 1, 20, 0
        repo = TenantScopedRepository(Report, self.session)
        query = repo.query()
        if status in _PAGE_STEPS:
            query = query.filter(Report.status == status)
        if days:
            query = query.filter(Report.created_at >= now_local() - timedelta(days=days))
        if q:
            like = f"%{str(q).strip()}%"
            query = query.filter(db_or(Report.id.ilike(like), Report.summary_line.ilike(like)))
        rows_all = query.order_by(Report.created_at.desc(), Report.id.desc()).all()
        total = len(rows_all)
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        window = rows_all[(page - 1) * size: page * size]
        quota = self._quota()
        return {"total": total, "pages": pages, "page": page,
                "rows": [{"id": r.id, "status": r.status, "step": r.step or r.status,
                          "summary_line": r.summary_line or "",
                          "window_from": r.window_from.strftime("%Y-%m-%d") if r.window_from else "",
                          "window_to": r.window_to.strftime("%Y-%m-%d") if r.window_to else "",
                          "created_at": hm_string(r.created_at),
                          "share_token": (r.share_token or "") if r.share_active else "",
                          "share_expires": hm_string(r.share_expires) if r.share_expires else "",
                          # 前端分享下拉需要嵌套 share 对象（diagShareMenu 读 r.share）
                          "share": ({"active": True,
                                     "token": r.share_token or "",
                                     "expires_at": hm_string(r.share_expires) if r.share_expires else ""}
                                    if r.share_active else None)}
                         for r in window],
                "quota": quota}

    @staticmethod
    def _is_new_month(period_start) -> bool:
        """period_start 在上个自然月之前（NULL 视为未登记，不算跨月）。"""
        from datetime import date

        if period_start is None:
            return False
        return period_start < date.today().replace(day=1)

    def _quota(self) -> dict:
        """配额视图。**GET 不写库**：跨月只把显示口径归零，落库由 generate 负责。

        之前在 GET 路径里 commit —— GET 有写副作用，且会提前提交整个请求会话
        （可能捎带提交未完成的脏对象）。
        """
        row = TenantScopedRepository(ReportQuota, self.session).first()
        used = int(row.used) if row else 0
        if self._is_new_month(getattr(row, "period_start", None)):
            used = 0
        return {"limit": int(row.quota_limit) if row else 25, "used": used,
                "total_cost": round(float(row.total_cost), 2) if row else 0.0}

    # ── 生成 / 重试 ─────────────────────────────────────────────────────────

    def generate(self) -> tuple[dict, int]:
        tid = current_tenant()
        qrepo = TenantScopedRepository(ReportQuota, self.session)
        quota = qrepo.first()
        if quota is None:
            quota = qrepo.add(ReportQuota(tenant_id=tid, quota_limit=25, used=0, total_cost=0.0))
            self.session.commit()
        # 快速拒绝（避免为注定失败的请求去采集数据）；真正扣减在下面原子执行
        if not self._is_new_month(quota.period_start) \
                and int(quota.used) >= int(quota.quota_limit):
            return ({"ok": False,
                     "error": f"报告额度已用完（{quota.used}/{quota.quota_limit}），请联系运营调高额度"}, 400)

        data = report_gen.collect_data(self.session)
        from app.services import geo_monitor
        geo_overall = geo_monitor.dashboard(session=self.session).get("overall") or {}
        reason = report_gen.data_gate(data, geo_overall)
        if reason:
            return ({"ok": False, "error": reason}, 400)

        now = now_local()
        tenant = directory.get_tenant(tid)
        repo = TenantScopedRepository(Report, self.session)
        row = repo.add(Report(id=report_gen.new_report_id(tid, now),
                              title=(tenant or {}).get("name", tid),
                              window_from=None, window_to=None, status="queued",
                              step="queued"))
        row.window_from = _iso_date(data["window_from"])
        row.window_to = _iso_date(data["window_to"])
        self.session.flush()               # 先落 Report 行，再原子扣额度
        from datetime import date

        # 并发下到这一步才真正分出胜负（原子扣减在 services 层：V1 不许蓝图裸 execute）
        if not report_gen.consume_quota(self.session, tid,
                                        first=date.today().replace(day=1)):
            self.session.rollback()
            return ({"ok": False,
                     "error": "报告额度已用完（并发抢占），请联系运营调高额度"}, 400)
        self.session.commit()

        from app.platform import jobs
        jobs.enqueue("report.generate", {"report_id": row.id}, tenant_id=tid, queue="report")
        return ({"ok": True, "report_id": row.id}, 200)

    def retry(self, report_id: str) -> tuple[dict, int]:
        row = self._get(report_id)
        if row.status != "failed":
            return ({"ok": False, "error": "只有失败的报告可以重试"}, 400)
        row.status, row.step, row.note = "queued", "queued", None
        self.session.commit()
        from app.platform import jobs
        jobs.enqueue("report.generate", {"report_id": row.id},
                     tenant_id=current_tenant(), queue="report")
        return ({"ok": True, "report_id": row.id}, 200)

    # ── 状态 / 正文 ─────────────────────────────────────────────────────────

    def status(self, report_id: str) -> tuple[dict, int]:
        row = self._get(report_id)
        return ({"ok": True, "status": row.status, "step": row.step or row.status,
                 "fail_reason": row.note if row.status == "failed" else ""}, 200)

    def html(self, report_id: str) -> str:
        row = self._get(report_id)
        content = report_gen.read_report_html(row.html_path)
        if content is None:
            raise NotFound("报告正文尚未生成或文件缺失")
        return content

    def shared_html(self, token: str) -> str | None:
        """公开分享正文：token 即授权（匿名外访，原站语义）。

        裸查与文件解析下沉在 `app/services/report_share.py`（blueprints 下
        禁裸查，V1；公开域只读查询同 directory 先例归 services 层）。
        撤回 / 过期 / token 不存在一律返回 None → 路由层 404。
        """
        from app.services.report_share import shared_html
        return shared_html(token)

    # ── 分享 ────────────────────────────────────────────────────────────────

    def share(self, report_id: str, days: int = 30) -> tuple[dict, int]:
        row = self._get(report_id)
        if row.status != "done":
            return ({"ok": False, "error": "报告完成后才能分享"}, 400)
        try:
            days = max(1, min(int(days), 365))
        except (TypeError, ValueError):
            days = 30
        # token 无唯一约束（存量库/迁移都不加），故在签发时保证不碰撞：
        # 撞车会让 /r/<token> 读到别的租户的报告正文。唯一性检查在
        # services/report_share.new_share_token（blueprints 禁止裸查）。
        token = report_share.new_share_token()
        if not token:
            return ({"ok": False, "error": "生成分享链接失败，请重试"}, 500)
        row.share_token = token
        row.share_active = True
        row.share_enabled = True
        row.share_expires = now_local() + timedelta(days=days)
        self.session.commit()
        return ({"ok": True, "token": row.share_token,
                 "expires_at": hm_string(row.share_expires)}, 200)

    def revoke_share(self, token: str) -> tuple[dict, int]:
        row = (TenantScopedRepository(Report, self.session).query()
               .filter(Report.share_token == str(token or "")).first())
        if row is None:
            return ({"ok": False, "error": "分享不存在"}, 404)
        row.share_active = False
        self.session.commit()
        return ({"ok": True}, 200)

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _get(self, report_id: str) -> Report:
        row = TenantScopedRepository(Report, self.session).first(id=str(report_id or ""))
        if row is None:
            raise NotFound("报告不存在")
        return row


def db_or(*conditions):
    from sqlalchemy import or_
    return or_(*conditions)


def _iso_date(text: str):
    from datetime import date
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        return None
