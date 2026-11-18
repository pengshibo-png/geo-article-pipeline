"""Phase 4 补齐功能：报告配额自然月重置 / 抖音循环发布调度 /
客户画像 AI 洞察 / 同行挖掘。

异步功能（洞察、挖掘）走 API 入队 + 轮询状态端点，与前端契约一致。
"""
from __future__ import annotations

import time
from datetime import date

from app.extensions import db
from app.tenancy import tenant_scope
from tests.conftest import T1, login


# ── 报告配额自然月重置 ──────────────────────────────────────────────────────

def _last_month_first() -> date:
    today = date.today()
    first = today.replace(day=1)
    # 上月 1 日
    y, m = (first.year, first.month - 1) if first.month > 1 else (first.year - 1, 12)
    return date(y, m, 1)


def test_quota_shows_zero_across_month_without_writing(client, app):
    """跨月：GET 只把显示口径归零，**不写库**（GET 不应有写副作用）。"""
    from app.models import ReportQuota

    login(client, "alpha", "pw-alpha")
    with app.app_context(), tenant_scope(T1):
        db.session.add(ReportQuota(tenant_id=T1, quota_limit=25, used=7,
                                   total_cost=0.14, period_start=_last_month_first()))
        db.session.commit()
    q = client.get("/api/report/list").get_json()["quota"]
    assert q["used"] == 0, "跨月显示应归零"
    assert q["total_cost"] == 0.14      # 累计成本不随月清零
    with app.app_context(), tenant_scope(T1):
        row = db.session.get(ReportQuota, T1)
        assert row.used == 7 and row.period_start == _last_month_first(), \
            "GET 不应修改库"


def test_quota_consume_is_atomic_and_resets(client, app):
    """真正扣减走单条 UPDATE：并发下不会超额，且顺带跨月重置。"""
    from app.models import ReportQuota

    with app.app_context(), tenant_scope(T1):
        db.session.add(ReportQuota(tenant_id=T1, quota_limit=25, used=7,
                                   total_cost=0.14, period_start=_last_month_first()))
        db.session.commit()
        from app.extensions import db as _db
        from app.services import report_gen

        first = date.today().replace(day=1)
        assert report_gen.consume_quota(_db.session, T1, first=first) is True
        row = _db.session.get(ReportQuota, T1)
        assert row.used == 1, "跨月后本次应从 1 起算（旧 used=7 已作废）"
        assert row.period_start == first
        assert row.total_cost == 0.16

        # 额度耗尽：原子 UPDATE 拒绝（rowcount=0），不产生负数也不超额
        row.quota_limit, row.used = 1, 1
        _db.session.commit()
        assert report_gen.consume_quota(_db.session, T1, first=first) is False
        assert _db.session.get(ReportQuota, T1).used == 1, "拒绝时不该改动计数"


def test_quota_not_reset_within_same_month(client, app):
    from app.models import ReportQuota

    login(client, "alpha", "pw-alpha")
    with app.app_context(), tenant_scope(T1):
        db.session.add(ReportQuota(tenant_id=T1, quota_limit=25, used=3,
                                   total_cost=0.06,
                                   period_start=date.today().replace(day=1)))
        db.session.commit()
    q = client.get("/api/report/list").get_json()["quota"]
    assert q["used"] == 3


def test_quota_first_use_registers_period(client, app):
    """period_start=NULL（老数据）→ 首次登记当月，不清零。"""
    from app.models import ReportQuota

    login(client, "alpha", "pw-alpha")
    with app.app_context(), tenant_scope(T1):
        db.session.add(ReportQuota(tenant_id=T1, quota_limit=25, used=2,
                                   total_cost=0.04, period_start=None))
        db.session.commit()
    q = client.get("/api/report/list").get_json()["quota"]
    assert q["used"] == 2


# ── 抖音循环发布：调度器真触发 ──────────────────────────────────────────────

def _douyin_fixture(app, **over):
    from app.services import douyin_store

    with app.app_context(), tenant_scope(T1):
        douyin_store.write_pack({"id": 1, "title": "待发包", "images": []})
        cfg = {"enabled": True, "interval_min": 30, "account_ids": ["acc-1"],
               "order": "new", "auto_gen": True, "gen_mode": "stock",
               "gen_batch": 5, "last_run": "2026-10-13 00:00:00"}
        cfg.update(over)
        douyin_store.save_autopub(cfg)


def test_scheduler_fires_douyin_autopub(app, monkeypatch):
    from datetime import datetime

    from app.services import scheduler

    calls = []
    monkeypatch.setattr("app.platform.jobs.enqueue",
                        lambda name, payload, **kw: calls.append((name, payload)))
    _douyin_fixture(app)
    with app.app_context():
        res = scheduler.tick(now=datetime(2026, 10, 14, 12, 0, 0))     # 已过间隔
    assert any(c[0] == "douyin.publish.batch" for c in calls)
    assert "t_alpha:douyin:publish:1" in res["fired"]


def test_scheduler_douyin_skips_when_not_due(app, monkeypatch):
    from datetime import datetime

    from app.services import scheduler

    calls = []
    monkeypatch.setattr("app.platform.jobs.enqueue",
                        lambda name, payload, **kw: calls.append((name, payload)))
    _douyin_fixture(app, last_run="2026-10-14 11:59:00")
    with app.app_context():
        res = scheduler.tick(now=datetime(2026, 10, 14, 12, 0, 0))
    assert not any(c[0].startswith("douyin.") for c in calls)
    assert "t_alpha:douyin:publish:1" not in res["fired"]


def test_scheduler_douyin_autogen_when_stock_empty(app, monkeypatch):
    """库存空 → 按配置自动补产（gen_mode≠off）。"""
    from datetime import datetime

    from app.services import douyin_store, scheduler

    calls = []
    monkeypatch.setattr("app.platform.jobs.enqueue",
                        lambda name, payload, **kw: calls.append((name, payload)))
    with app.app_context(), tenant_scope(T1):
        douyin_store.save_autopub({"enabled": True, "interval_min": 30,
                                   "account_ids": ["acc-1"], "auto_gen": True,
                                   "gen_mode": "stock", "gen_batch": 5,
                                   "last_run": "2026-10-13 00:00:00"})
    with app.app_context():
        res = scheduler.tick(now=datetime(2026, 10, 14, 12, 0, 0))
    assert any(c[0] == "douyin.generate.batch" for c in calls)
    assert "t_alpha:douyin:generate" in res["fired"]


def test_douyin_autopub_save_not_immediate(app):
    """保存计划 ≠ 立即跑：首次启用登记 last_run=现在。"""
    from app.blueprints.generation.douyin_service import DouyinService
    from app.services import douyin_store

    with app.app_context(), tenant_scope(T1):
        DouyinService().autopub_save({"enabled": True, "account_ids": ["acc-1"]})
        cfg = douyin_store.load_autopub()
    assert cfg["enabled"] and cfg.get("last_run")


# ── 客户画像 AI 洞察（异步）────────────────────────────────────────────────

def test_audience_analyze_async_loop(client, app):
    login(client, "alpha", "pw-alpha")
    from app.models import Audience

    with app.app_context(), tenant_scope(T1):
        db.session.add(Audience(tenant_id=T1, content="【人群】25-40 岁女性"))
        db.session.commit()
    r = client.post("/api/audience/analyze", json={})
    assert r.get_json().get("ok") is True
    s = _poll_audience(client)
    assert s["error"] is None, s
    assert s["result"], "洞察应非空"
    # 落盘后 GET /api/audience 能读回（页面据此显示"已生成"）
    assert client.get("/api/audience").get_json()["insight"] == s["result"]


def test_audience_analyze_rejects_concurrent(client, app):
    from app.blueprints.config import audience_jobs
    from app.models import Audience

    login(client, "alpha", "pw-alpha")
    with app.app_context(), tenant_scope(T1):
        db.session.add(Audience(tenant_id=T1, content="【人群】x"))
        db.session.commit()
        audience_jobs.analyze_job({})       # 直接跑一次，结束后再测并发
    with app.app_context(), tenant_scope(T1):
        audience_jobs._st(T1)["analyzing"] = True
    r = client.post("/api/audience/analyze", json={})
    assert r.get_json()["ok"] is False
    with app.app_context(), tenant_scope(T1):
        audience_jobs._st(T1)["analyzing"] = False


def test_audience_insight_survives_profile_save(client, app):
    """保存画像不能把已有洞察清掉（页面回传才覆盖）。"""
    from app.services import audience_store

    login(client, "alpha", "pw-alpha")
    with app.app_context(), tenant_scope(T1):
        audience_store.save_insight("【人群特征】- 旧洞察")
    client.post("/api/audience", json={"profile": {"singles": {"who": "宝妈"}}})
    assert client.get("/api/audience").get_json()["insight"] == "【人群特征】- 旧洞察"


# ── 同行挖掘（异步 → CompetitorTitle 学习库）───────────────────────────────

def test_scrape_competitors_fills_learning_library(client, app):
    from app.models import CompetitorTitle

    login(client, "alpha", "pw-alpha")
    r = client.post("/api/titles/scrape-competitors", json={"background": True})
    assert r.get_json().get("ok") is True
    s = _poll_scrape(client)
    assert s["error"] is None, s
    assert s["captured"] > 0
    with app.app_context(), tenant_scope(T1):
        n = len(CompetitorTitle.query.filter_by(tenant_id=T1).all())
    assert n >= s["captured"]
    status = client.get("/api/titles/scrape-status").get_json()
    assert status["library"] == n


def test_scrape_dedupes_titles(app):
    """重复标题不重复入库（学习库不该无限膨胀）。"""
    from app.models import CompetitorTitle

    with app.app_context(), tenant_scope(T1):
        from app.blueprints.topic import scrape_jobs

        n1 = scrape_jobs._store_titles(["标题甲", "标题乙"])
        n2 = scrape_jobs._store_titles(["标题甲", "标题丙"])   # 甲已存在
        rows = CompetitorTitle.query.filter_by(tenant_id=T1).all()
    assert n1 == 2 and n2 == 1, "第二轮只有新标题入库"
    assert len(rows) == 3


def test_scrape_library_capped_evicts_oldest(app, monkeypatch):
    """学习库有上限：超出淘汰最旧（滚动窗口，不是无限堆积）。"""
    from app.models import CompetitorTitle

    with app.app_context(), tenant_scope(T1):
        from app.blueprints.topic import scrape_jobs

        monkeypatch.setattr(scrape_jobs, "_LIBRARY_CAP", 5)
        scrape_jobs._store_titles([f"t{i}" for i in range(4)])
        scrape_jobs._store_titles([f"n{i}" for i in range(3)])   # 7 > 5 → 淘汰 2 条最旧
        rows = sorted(CompetitorTitle.query.filter_by(tenant_id=T1).all(),
                      key=lambda r: r.id)
    assert len(rows) == 5
    assert [r.text for r in rows] == ["t2", "t3", "n0", "n1", "n2"]


def test_scrape_rejects_concurrent(client, app):
    from app.blueprints.topic import scrape_jobs

    login(client, "alpha", "pw-alpha")
    with app.app_context(), tenant_scope(T1):
        scrape_jobs._st(T1)["running"] = True
    r = client.post("/api/titles/scrape-competitors", json={})
    assert r.get_json()["ok"] is False
    with app.app_context(), tenant_scope(T1):
        scrape_jobs._st(T1)["running"] = False


# ── CSRF（自研，零依赖）────────────────────────────────────────────────────

def _csrf_on(app):
    app.config["CSRF_ENABLED"] = True


def _csrf_off(app):
    app.config["CSRF_ENABLED"] = False


def test_csrf_issues_token_and_blocks_untokened_post(client, app):
    login(client, "alpha", "pw-alpha")
    _csrf_on(app)
    try:
        tok = client.get("/api/csrf").get_json()["token"]
        assert tok and len(tok) > 20
        r = client.post("/api/titles", json={"mode": "append", "titles": "未带令牌"})
        assert r.status_code == 403, "非安全方法无令牌必须拒绝"
    finally:
        _csrf_off(app)


def test_csrf_allows_post_with_token(client, app):
    login(client, "alpha", "pw-alpha")
    _csrf_on(app)
    try:
        tok = client.get("/api/csrf").get_json()["token"]
        r = client.post("/api/titles", json={"mode": "append", "titles": "带令牌"},
                        headers={"X-CSRFToken": tok})
        assert r.status_code == 200
    finally:
        _csrf_off(app)


def test_csrf_rejects_wrong_token(client, app):
    login(client, "alpha", "pw-alpha")
    _csrf_on(app)
    try:
        client.get("/api/csrf")
        r = client.post("/api/titles", json={"mode": "append", "titles": "假令牌"},
                        headers={"X-CSRFToken": "not-the-real-token"})
        assert r.status_code == 403
    finally:
        _csrf_off(app)


def test_csrf_token_survives_login(client, app):
    """登录会 session.clear() —— 令牌必须跨登录存活，否则登录后的第一个 POST 被自己拦。"""
    _csrf_on(app)
    try:
        tok = client.get("/api/csrf").get_json()["token"]      # 匿名会话先拿令牌
        r = client.post("/api/auth/login",
                        json={"username": "alpha", "password": "pw-alpha"},
                        headers={"X-CSRFToken": tok})
        assert r.get_json().get("ok") is True, r.get_json()
        r2 = client.post("/api/titles", json={"mode": "append", "titles": "登录后"},
                         headers={"X-CSRFToken": tok})
        assert r2.status_code == 200, "同一个令牌在登录后应继续有效"
    finally:
        _csrf_off(app)


def test_csrf_get_requests_never_blocked(client, app):
    """安全方法（GET）不做 CSRF 校验 —— 否则正常浏览会被 403。"""
    login(client, "alpha", "pw-alpha")
    _csrf_on(app)
    try:
        assert client.get("/api/me").status_code == 200
        assert client.get("/api/geo/dashboard").status_code == 200
    finally:
        _csrf_off(app)


# ── 进程内状态桥：有界、驱逐最久未访问 ─────────────────────────────────────

def test_state_bridge_evicts_least_recently_used():
    """裸 dict 每租户一条永不回收 = 内存泄漏；桥要有上限并驱逐最久未访问的。"""
    from app.platform.state_bridge import StateBridge

    b = StateBridge(lambda: {"v": 0}, cap=4)
    for i in range(4):
        b.get(f"t{i}")
    assert len(b) == 4
    b.get("t0")                       # 把 t0 刷新成最近访问
    b.get("t9")                       # 触发驱逐：一次性清 1/4（1 条，最旧是 t1）
    assert "t9" in b and "t1" not in b
    assert "t0" in b                  # 刚访问过的不该被驱逐
    assert len(b) == 4


def test_state_bridge_returns_same_object():
    """同一租户反复 get 拿到同一个 dict（否则进度状态会丢）。"""
    from app.platform.state_bridge import StateBridge

    b = StateBridge(lambda: {"v": 0})
    a = b.get("t1")
    a["v"] = 7
    assert b.get("t1")["v"] == 7


# ── PostgreSQL 行级安全：租户上下文注入 ─────────────────────────────────────

def test_rls_hook_injects_tenant_context(monkeypatch):
    """切 PG 后若没有这个钩子，RLS 会把所有租户查询拦成 0 行（比不做 RLS 更致命）。

    这里用 SQLite 引擎伪装成 postgresql 方言，捕获事务开启时的 SQL。
    """
    import sqlalchemy as sa
    from sqlalchemy.engine import Connection

    from app.tenancy import install_rls_tenant_hook

    eng = sa.create_engine("sqlite://")
    install_rls_tenant_hook(eng)
    monkeypatch.setattr(eng.dialect, "name", "postgresql", raising=False)

    calls = []
    orig = Connection.exec_driver_sql

    def spy(self, sql, *a, **kw):
        if "set_config" in str(sql):
            calls.append((str(sql), a))
            return None          # 不真下发给驱动：%s 是 psycopg 占位符，sqlite 不认
        return orig(self, sql, *a, **kw)

    monkeypatch.setattr(Connection, "exec_driver_sql", spy)

    with tenant_scope(T1):
        with eng.begin() as conn:
            conn.exec_driver_sql("SELECT 1")

    assert calls, "事务开启时应注入租户上下文"
    sql, args = calls[0]
    assert "set_config" in sql and "app.tenant_id" in sql
    assert args[0][0] == T1
    assert "true" in sql            # is_local=true → 事务级，不串到下一个请求


def test_rls_hook_skips_without_tenant(monkeypatch):
    """无租户上下文（迁移/DDL）不注入 —— 对 RLS 而言是 fail-closed。"""
    import sqlalchemy as sa
    from sqlalchemy.engine import Connection

    from app.tenancy import install_rls_tenant_hook

    eng = sa.create_engine("sqlite://")
    install_rls_tenant_hook(eng)
    monkeypatch.setattr(eng.dialect, "name", "postgresql", raising=False)
    calls = []
    orig = Connection.exec_driver_sql

    def spy(self, sql, *a, **kw):
        if "set_config" in str(sql):
            calls.append(sql)
        return orig(self, sql, *a, **kw)

    monkeypatch.setattr(Connection, "exec_driver_sql", spy)
    with eng.begin() as conn:
        conn.exec_driver_sql("SELECT 1")
    assert calls == []


def test_rls_hook_skips_on_sqlite(monkeypatch):
    """SQLite 无 RLS：不执行任何 set_config（方言判定）。"""
    import sqlalchemy as sa
    from sqlalchemy.engine import Connection

    from app.tenancy import install_rls_tenant_hook

    eng = sa.create_engine("sqlite://")
    install_rls_tenant_hook(eng)
    calls = []
    orig = Connection.exec_driver_sql

    def spy(self, sql, *a, **kw):
        if "set_config" in str(sql):
            calls.append(sql)
        return orig(self, sql, *a, **kw)

    monkeypatch.setattr(Connection, "exec_driver_sql", spy)
    with tenant_scope(T1):
        with eng.begin() as conn:
            conn.exec_driver_sql("SELECT 1")
    assert calls == []


# ── helpers ──────────────────────────────────────────────────────────────────

def _poll_audience(client, timeout: float = 60) -> dict:
    end = time.monotonic() + timeout
    last = {}
    while time.monotonic() < end:
        last = client.get("/api/audience/analyze-status").get_json()
        if not last["analyzing"]:
            return last
        time.sleep(0.4)
    return last


def _poll_scrape(client, timeout: float = 60) -> dict:
    end = time.monotonic() + timeout
    last = {}
    while time.monotonic() < end:
        last = client.get("/api/titles/scrape-status").get_json()
        if not last["running"]:
            return last
        time.sleep(0.4)
    return last
