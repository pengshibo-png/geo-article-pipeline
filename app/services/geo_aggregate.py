"""GEO 聚合口径（GeoResult → GeoDaily → dashboard 形状）——唯一出口。

从 geo_monitor.py 拆出（A3 行数预算），两边都从这里导入：
- recompute_daily：按自然日重算各引擎日行（upsert，AI-ARCH-002 口径）
- dashboard：看板形状（参考系统看板接口 冻结），无数据显示空形状不伪造。
"""
from __future__ import annotations

from datetime import date

from app.models import GeoDaily, GeoResult
from app.tenancy import TenantScopedRepository, current_tenant
from app.timeutil import hm_string, now_local

_TREND_DAYS = 30


def recompute_daily(day: date | None = None, session=None) -> int:
    """按当前租户重算指定自然日的各引擎日行（upsert）。返回写入行数。"""
    from app.extensions import db

    session = session or db.session
    day = day or now_local().date()
    tid = current_tenant()
    rows = TenantScopedRepository(GeoResult, session).all()
    today_rows = [r for r in rows
                  if r.error is None and r.sampled_at is not None
                  and r.sampled_at.date() == day]
    repo = TenantScopedRepository(GeoDaily, session)
    existing = {(r.engine, r.channel): r for r in repo.all() if r.date == day}
    n = 0
    groups: dict = {}
    for r in today_rows:
        groups.setdefault((r.engine, r.channel), []).append(r)
    for (engine, channel), items in groups.items():
        valid = len(items)
        cnt = lambda f: sum(1 for i in items if f(i))  # noqa: E731
        stats = {
            "valid": valid,
            "mention_rate": round(cnt(lambda i: i.mentioned) / valid * 100, 1) if valid else 0.0,
            "top_rate": round(cnt(lambda i: i.rank == 1) / valid * 100, 1) if valid else 0.0,
            "top3_rate": round(cnt(lambda i: i.rank is not None and i.rank <= 3) / valid * 100,
                               1) if valid else 0.0,
            "cite_rate": round(cnt(lambda i: i.cited_lv) / valid * 100, 1) if valid else 0.0,
        }
        row = existing.get((engine, channel))
        if row is None:
            row = GeoDaily(date=day, engine=engine, channel=channel, tenant_id=tid)
            repo.add(row)
        for k, v in stats.items():
            setattr(row, k, v)
        n += 1
    return n


def _metrics(rows) -> dict:
    valid = sum(int(r.valid) for r in rows)
    if not valid:
        return {"valid": 0, "mention_rate": 0, "top_rate": 0, "top3_rate": 0, "cite_rate": 0}

    def wattr(name):
        return round(sum(float(getattr(r, name)) * int(r.valid) for r in rows) / valid, 1)

    return {"valid": valid, "mention_rate": wattr("mention_rate"), "top_rate": wattr("top_rate"),
            "top3_rate": wattr("top3_rate"), "cite_rate": wattr("cite_rate")}


def dashboard(session=None) -> dict:
    """date/overall/per_engine/trend/cited_ours（root/geo/citations/reports 共用）。"""
    from app.extensions import db
    from app.models import Article

    session = session or db.session
    rows = TenantScopedRepository(GeoDaily, session).query().order_by(
        GeoDaily.date.asc(), GeoDaily.engine.asc()).all()
    if not rows:
        return {"date": "", "overall": {}, "per_engine": {}, "trend": [],
                "cited_ours": [], "sov": {"total": 0, "ours": 0, "rate": 0}}

    latest = rows[-1].date
    day_rows = [r for r in rows if r.date == latest]
    per_engine = {r.engine: {"valid": int(r.valid), "mention_rate": round(float(r.mention_rate), 1),
                             "top_rate": round(float(r.top_rate), 1),
                             "top3_rate": round(float(r.top3_rate), 1),
                             "cite_rate": round(float(r.cite_rate), 1)} for r in day_rows}
    dates = sorted({r.date for r in rows})[-_TREND_DAYS:]
    trend = []
    for d in dates:
        m = _metrics([r for r in rows if r.date == d])
        trend.append({"date": d.strftime("%Y-%m-%d"),
                      "mention_rate": m["mention_rate"], "cite_rate": m["cite_rate"]})

    art_rows = TenantScopedRepository(Article, session).all()
    dir_title = {a.dir: a.title or a.dir for a in art_rows}
    cited_rows = [r for r in TenantScopedRepository(GeoResult, session).all()
                  if r.cited_lv and r.cited_url]
    cited_rows.sort(key=lambda r: r.sampled_at, reverse=True)
    cited_ours = [{"engine": r.engine, "platform": _platform_of(r.cited_url),
                   "query": r.query, "title": _title_of(r.cited_url, dir_title),
                   "url": r.cited_url, "time": hm_string(r.sampled_at)}
                  for r in cited_rows[:20]]

    # AI 引用份额(Share of Voice)：全量被引记录 / 全量品牌提及数 —— 环比展示
    # "品牌提及中有多少引用了您的内容"。系统只跟踪自有内容的被引（不爬竞品），
    # 故 total=品牌提及数、ours=被引数、rate=被引率（与 overall.cite_rate 一致）。
    cited_full = TenantScopedRepository(GeoResult, session).query().filter_by(
        cited_lv=True).count()
    mentioned_full = TenantScopedRepository(GeoResult, session).query().filter_by(
        mentioned=True).count()
    sov = {"total": int(mentioned_full), "ours": int(cited_full),
           "rate": round(float(_metrics(day_rows).get("cite_rate", 0)), 1)}

    return {"date": latest.strftime("%Y-%m-%d"), "overall": _metrics(day_rows),
            "per_engine": per_engine, "trend": trend,
            "cited_ours": cited_ours, "sov": sov}


def _platform_of(url: str) -> str:
    for plat in ("meipian", "souhu", "baijiahao", "lieju", "wuba"):
        if plat in (url or ""):
            return plat
    return "other"


def _title_of(url: str, dir_title: dict) -> str:
    """被引 URL 若能对上本租户文章目录名 → 用文章标题。"""
    for d, t in dir_title.items():
        if d and d in (url or ""):
            return t
    return (url or "").split("/")[-1] or "未知来源"
