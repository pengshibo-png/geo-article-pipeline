"""topic 蓝图 · 选题库与调度（titles.html 客户页对接面）。

页面契约（参考系统线上行为实测）：
- GET  /api/titles → {enriched:[{title,source,time}], library, pending, done}
- POST /api/titles {mode:'append', titles, source} → {ok, added}；{mode:'single_delete', title}
- GET/POST /api/autosched → {title_sched, article_sched, titles_left}
删除是**软删**（deleted_at，ADR 问题 12），硬删会撞 partial 唯一索引的语义。
"""
from __future__ import annotations

import json

from app.errors import ValidationError
from app.extensions import db as _db
from app.models import Account, Article, CompetitorTitle, Schedule, Title
from app.timeutil import hm_string, now_local
from app.tenancy import TenantScopedRepository, current_tenant

_TITLE_SOURCES = ("ai", "manual")
_SCHED_DEFAULTS = {
    "title": {"time": "02:00", "every_min": 60, "count": 50},
    "article": {"time": "03:00", "every_min": 60, "count": 30},
}


def _int_in(value, low: int, high: int, fallback: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(n, high))


class TitlesService:
    """选题库读写（页面展示/手动维护）。"""

    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    def list_titles(self) -> dict:
        repo = TenantScopedRepository(Title, self.session)
        rows = (repo.query().filter(Title.deleted_at.is_(None))
                .order_by(Title.created_at.desc(), Title.id.desc()).all())
        enriched = [{"title": r.text, "source": r.source if r.source in _TITLE_SOURCES else "manual",
                     "time": hm_string(r.created_at)} for r in rows]
        pending = sum(1 for r in rows if not r.used)
        return {
            "enriched": enriched,
            "pending": pending,
            "done": len(rows) - pending,
            "library": TenantScopedRepository(CompetitorTitle, self.session).query().count(),
        }

    def add_titles(self, text: str, source: str = "manual") -> dict:
        """按行拆分入库；批内/库内重复与空行跳过（页面语义是"已添加 N 条"）。"""
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("titles 不能为空")
        repo = TenantScopedRepository(Title, self.session)
        existing = {r.text for r in repo.query().filter(Title.deleted_at.is_(None)).all()}
        added = 0
        for line in text.replace("\r\n", "\n").split("\n"):
            t = line.strip(" 　\t")[:255]
            if not t or t in existing:
                continue
            repo.add(Title(text=t, source=source if source in _TITLE_SOURCES else "manual",
                           used=False))
            existing.add(t)
            added += 1
        self.session.commit()
        return {"ok": True, "added": added}

    def delete_title(self, text: str) -> dict:
        """软删：同名（同租户未删）全部标记 deleted_at。"""
        repo = TenantScopedRepository(Title, self.session)
        rows = (repo.query()
                .filter(Title.text == str(text or ""), Title.deleted_at.is_(None)).all())
        stamp = now_local()
        for r in rows:
            r.deleted_at = stamp
        self.session.commit()
        return {"ok": True, "deleted": len(rows)}


class AutoschedService:
    """选题/文章生成计划 + 发布计划（titles/gen/tasks 三页共用 /api/autosched）。

    计划**落库**（Schedule 表：title/article 各一行用列存参数；publish 计划是
    每账号数组，整体 JSON 存 kind="publish" 行的 config 列）。执行器在 Phase III
    调度线程消费（next_run_at 到期触发）；页面保存后读回一致即为真实契约。
    """

    _KEYS = (("title_sched", "title"), ("article_sched", "article"))

    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    def get(self) -> dict:
        repo = TenantScopedRepository(Schedule, self.session)
        rows = {r.kind: r for r in repo.all()}
        out = {key: self._shape(rows.get(kind), kind=kind) for key, kind in self._KEYS}
        out["publish_plans"] = self._publish_plans(rows.get("publish"))
        accounts = TenantScopedRepository(Account, self.session).all()
        out["accounts"] = [{"id": a.id, "name": a.name, "platform": a.platform}
                           for a in accounts if a.enabled and a.status == "active"
                           and int(a.daily_limit) > 0]
        art_repo = TenantScopedRepository(Article, self.session)
        out["articles_ready"] = art_repo.query().filter(Article.status == "final").count()
        left_repo = TenantScopedRepository(Title, self.session)
        out["titles_left"] = (left_repo.query()
                              .filter(Title.used.is_(False), Title.deleted_at.is_(None)).count())
        return out

    @staticmethod
    def _publish_plans(row: Schedule | None) -> list:
        if row is None or not (row.config or "").strip():
            return []
        try:
            data = json.loads(row.config)
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def _shape(self, row: Schedule | None, *, kind: str = "title") -> dict:
        d = _SCHED_DEFAULTS[kind]
        if row is None:
            return {"enabled": False, "mode": "daily", **d}
        return {"enabled": bool(row.enabled), "mode": row.mode if row.mode in ("daily", "interval") else "daily",
                "time": row.at_time or d["time"],
                "every_min": _int_in(row.every_min, 5, 1440, d["every_min"]),
                "count": _int_in(row.count, 1, 500, d["count"])}

    def save(self, body: dict) -> dict:
        repo = TenantScopedRepository(Schedule, self.session)
        for key, kind in self._KEYS:
            data = body.get(key)
            if not isinstance(data, dict):
                continue
            d = _SCHED_DEFAULTS[kind]
            row = repo.query().filter(Schedule.kind == kind).first()
            if row is None:
                row = repo.add(Schedule(kind=kind, mode="daily", enabled=False))
            row.enabled = bool(data.get("enabled"))
            row.mode = "interval" if data.get("mode") == "interval" else "daily"
            row.at_time = str(data.get("time") or d["time"])[:8]
            row.every_min = _int_in(data.get("every_min"), 5, 1440, d["every_min"])
            row.count = _int_in(data.get("count"), 1, 500, d["count"])
        if isinstance(body.get("publish_plans"), list):
            plans = [self._clean_plan(p) for p in body["publish_plans"] if isinstance(p, dict)]
            row = repo.query().filter(Schedule.kind == "publish").first()
            if row is None:
                row = repo.add(Schedule(kind="publish", mode="daily", enabled=False))
            row.config = json.dumps(plans, ensure_ascii=False)
        self.session.commit()
        return {"ok": True}

    @staticmethod
    def _clean_plan(p: dict) -> dict:
        """发布计划字段清洗（tasks.html ppAdd 构造的形状：channel/account/节奏）。"""
        mode = "interval" if p.get("mode") == "interval" else "daily"
        return {
            "id": _int_in(p.get("id"), 0, 10**9, 0),
            "channel": str(p.get("channel") or "")[:32],
            "account_id": str(p.get("account_id") or "")[:64],
            "account_name": str(p.get("account_name") or "")[:128],
            "count": _int_in(p.get("count"), 1, 500, 1),
            "time": str(p.get("time") or "08:30")[:8],
            "every_min": _int_in(p.get("every_min"), 1, 10080, 30),
            "enabled": bool(p.get("enabled")),
        }


def titles_left(tenant: str | None = None) -> int:
    """选题库余量（未使用且未删除）。独立小函数，gen 页 autosched 也用。"""
    del tenant  # 租户一律取当前上下文，参数仅为可读性占位
    repo = TenantScopedRepository(Title)
    return repo.query().filter(Title.used.is_(False), Title.deleted_at.is_(None)).count()


__all__ = ["TitlesService", "AutoschedService", "titles_left", "current_tenant"]
