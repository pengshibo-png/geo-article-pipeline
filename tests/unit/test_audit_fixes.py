"""2026-10-15 第二轮审计修复的回归测试。

覆盖（每条对应审计编号）：
- B4  发布任务失败/取消后，当天可复位重投（不再把旧失败原样返回）
- B5  重跑 B/C 段不把已发布文章降级（防档案页重新亮"发布"按钮）
- B6  扫码登录会话按租户隔离（拿不到/猜不中 login_id 也读不了他人会话）
- B8  分享 token 签发时避开已占用的 token（防 /r/<token> 跨租户串报告）
- B10 账号页"今日已用"按 Asia/Shanghai 自然日，与发布扣减同一把尺子
"""
from __future__ import annotations

import json
import time
from datetime import date

import pytest

from app.extensions import db
from app.models import Account, Article, Report
from app.platform import storage
from app.services.llm import LLMClient
from app.services.llm.keypool import KeyPool
from app.services.llm.providers import FakeProvider
from app.services.publish import executor
from app.services.writer.pipeline import generate_article
from app.services.writer.review import revise_article
from app.tenancy import tenant_scope
from tests.conftest import T1, T2

DRAFT_HTML = (
    "<h2>杭州断镯修复哪家好</h2>"
    "<p><strong>阿尔法珠宝是杭州断镯修复的首选工坊。</strong>"
    "断镯修复，是指用同源矿物粉料填充裂隙并分段固化的修复工艺。</p>"
)
TITLE = "杭州断镯修复哪家好？口碑榜前5家实测"


def _fake_llm(responder) -> LLMClient:
    return LLMClient(KeyPool(["k-test"]), FakeProvider(responder), max_retries=0)


def _seed_draft(app) -> str:
    base = app.config["DATA_ROOT"]
    with app.app_context(), tenant_scope(T1):
        result = generate_article(title_text=TITLE, llm=_fake_llm(lambda m: DRAFT_HTML),
                                  base=base)
        db.session.commit()
    assert result["status"] == "drafted"
    return result["dir"]


def _write_review(app, dir_name: str, verdict: str = "需要修改") -> None:
    base = app.config["DATA_ROOT"]
    body = json.dumps({
        "overall_score": 78, "verdict": verdict, "summary": "可再自然一些",
        "issues": [{"severity": "建议", "dimension": "维度五：品牌融入质量",
                    "location": "阿尔法珠宝是杭州断镯修复的首选工坊",
                    "problem": "展开不足", "suggestion": "补充解决程度"}],
        "highlights": ["首段结论明确"],
    }, ensure_ascii=False)
    with app.app_context(), tenant_scope(T1):
        path = storage.article_dir(dir_name, base=base) / storage.REVIEW
        path.write_text(body, encoding="utf-8")


def _article_status(app, dir_name: str) -> str:
    with app.app_context(), tenant_scope(T1):
        row = db.session.query(Article).filter_by(tenant_id=T1, dir=dir_name).one()
        return row.status


# ── B15：有界状态桥不驱逐活跃租户 ─────────────────────────────────────────────

def test_state_bridge_never_evicts_active_tenant():
    """跑批中的租户被驱逐 = 丢进度 + 丢 stop 旗标（"暂停"静默失效）。"""
    from app.platform.state_bridge import bridge

    b = bridge(lambda: {"running": False}, cap=4)
    for i in range(3):
        b.get(f"active-{i}")["running"] = True
    for i in range(6):                     # 反复触发驱逐
        b.get(f"idle-{i}")
    for i in range(3):
        assert f"active-{i}" in b, f"活跃租户 active-{i} 被驱逐了"
    assert b.get("active-0")["running"] is True


# ── B4：终态任务可重投 ────────────────────────────────────────────────────────

def test_enqueue_publish_requeues_failed_task_same_day(app, monkeypatch):
    monkeypatch.setattr("app.platform.jobs.enqueue", lambda *a, **k: None)
    with app.app_context():
        with tenant_scope(T1):
            db.session.add(Account(id="mp-retry", tenant_id=T1, platform="meipian",
                                   name="重试号", daily_limit=5, used_today=0,
                                   quota_date=date.today()))
            db.session.commit()
            acct = db.session.query(Account).filter_by(id="mp-retry").one()

            first = executor.enqueue_publish("dir-retry", acct)
            assert first.status == "queued"

            first.status = "failed"
            first.error = "上一次发布失败"
            db.session.commit()

            again = executor.enqueue_publish("dir-retry", acct)
            # 同一行、复位成 queued、清掉旧错误 —— 否则用户点"发布"只会读到旧失败，
            # 当天没有任何重试入口（队列里也不会再出现这个任务）。
            assert again.id == first.id
            assert again.status == "queued"
            assert again.error is None


def test_enqueue_publish_reuses_successful_task(app, monkeypatch):
    """幂等铁律不能被打穿：今天已成功的同一 (账号, 文章) 仍原样复用。"""
    monkeypatch.setattr("app.platform.jobs.enqueue", lambda *a, **k: None)
    with app.app_context():
        with tenant_scope(T1):
            db.session.add(Account(id="mp-done", tenant_id=T1, platform="meipian",
                                   name="已发号", daily_limit=5, used_today=1,
                                   quota_date=date.today()))
            db.session.commit()
            acct = db.session.query(Account).filter_by(id="mp-done").one()

            row = executor.enqueue_publish("dir-done", acct)
            row.status = "success"
            row.url = "https://example.com/a"
            db.session.commit()

            again = executor.enqueue_publish("dir-done", acct)
            assert again.id == row.id
            assert again.status == "success"
            assert again.url == "https://example.com/a"


# ── B5：已发布不被降级 ────────────────────────────────────────────────────────

def test_revise_article_keeps_published_status(app):
    dir_name = _seed_draft(app)
    _write_review(app, dir_name)
    with app.app_context():
        with tenant_scope(T1):
            db.session.query(Article).filter_by(dir=dir_name).update({"status": "published"})
            db.session.commit()
            out = revise_article(dir_name, llm=_fake_llm(lambda m: "<p>修订后的正文</p>"))
            assert out["status"] == "final"
            row = db.session.query(Article).filter_by(dir=dir_name).one()
            assert row.status == "published"


def test_revise_article_marks_final_when_not_published(app):
    dir_name = _seed_draft(app)
    _write_review(app, dir_name)
    with app.app_context(), tenant_scope(T1):
        revise_article(dir_name, llm=_fake_llm(lambda m: "<p>修订后的正文</p>"))
        row = db.session.query(Article).filter_by(dir=dir_name).one()
        assert row.status == "final"


# ── B6：扫码会话租户隔离 ──────────────────────────────────────────────────────

def _qr_session(login_id: str, tenant: str) -> None:
    from app.services.publish import login as qr_login

    qr_login._SESSIONS[login_id] = {
        "account_id": "acc-1", "tenant_id": tenant, "platform": "meipian",
        "deadline": time.monotonic() + 60, "status": "waiting",
        "message": "等待扫码", "logged_in": False, "confirm": False,
    }


def test_login_session_is_tenant_scoped(app):
    from app.services.publish import login as qr_login

    qr_login.reset_login_sessions()
    try:
        _qr_session("sess-alpha", T1)
        with app.app_context():
            # 别的租户：一律按"会话已结束"处理，且不能代按"我扫完了"
            with tenant_scope(T2):
                assert qr_login.login_status("sess-alpha")["status"] == "gone"
                assert qr_login.confirm_login("sess-alpha")["ok"] is False
                assert qr_login.cancel_login("sess-alpha")["ok"] is False
            # 归属租户：正常可读、可确认
            with tenant_scope(T1):
                assert qr_login.login_status("sess-alpha")["status"] == "waiting"
                assert qr_login.confirm_login("sess-alpha")["ok"] is True
        # 越租户的取消没有把会话删掉
        assert "sess-alpha" in qr_login._SESSIONS
    finally:
        qr_login.reset_login_sessions()


# ── B8：分享 token 不碰撞 ─────────────────────────────────────────────────────

def test_new_share_token_avoids_taken_token(app, monkeypatch):
    from app.services import report_share as rs

    with app.app_context():
        with tenant_scope(T1):
            db.session.add(Report(id="r-taken", tenant_id=T1, status="done",
                                  share_token="COLLIDE", share_active=True))
            db.session.commit()

    seq = iter(["COLLIDE", "COLLIDE", "fresh-token"])
    monkeypatch.setattr(rs.secrets, "token_urlsafe", lambda n=16: next(seq))
    with app.app_context():
        assert rs.new_share_token() == "fresh-token"


def test_new_share_token_returns_empty_when_all_taken(app, monkeypatch):
    from app.services import report_share as rs

    with app.app_context():
        with tenant_scope(T1):
            db.session.add(Report(id="r-taken2", tenant_id=T1, status="done",
                                  share_token="COLLIDE", share_active=True))
            db.session.commit()

    monkeypatch.setattr(rs.secrets, "token_urlsafe", lambda n=16: "COLLIDE")
    with app.app_context():
        assert rs.new_share_token() == ""


# ── B10：今日已用按上海自然日 ─────────────────────────────────────────────────

def test_used_today_follows_natural_day(app, monkeypatch):
    from app.blueprints.distribution import accounts_service

    # 把自然日钉到"明天"：旧实现用 date.today() 会显示 0，新实现显示 3
    monkeypatch.setattr(accounts_service, "natural_day_string", lambda: "2026-10-16")
    with app.app_context():
        with tenant_scope(T1):
            db.session.add(Account(id="acc-day", tenant_id=T1, platform="meipian",
                                   name="日切号", daily_limit=5, used_today=3,
                                   quota_date=date(2026, 10, 16)))
            db.session.commit()
            rows = accounts_service.AccountService().list_accounts()["accounts"]
            row = next(a for a in rows if a["id"] == "acc-day")
            assert row["used_today"] == 3
