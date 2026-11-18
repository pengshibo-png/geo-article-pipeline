"""抖音图文「每条作品带自己的发布时间」的回归测试（2026-10-19 新增）。

用户口径：排期表里每条图文有自己的时刻（12:25 / 19:25…），到点把**这一包发到
所有可用号**（矩阵铺满），一次性。定时发布无人值守，所以三条边界必须钉死：

- 到点才发、发过不重发（重发 = 同一篇图文铺两遍）；
- 迟到（应用没开着）在补发窗口内补发，超窗口记 `missed`（既不静默丢，也不
  在开机后一次灌出几十条）；
- 没账号 / 包被删 / 时间写坏，一律如实记账，而不是静静地什么也不做。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.errors import NotFound
from app.platform import jobs as jobs_mod
from app.services import douyin_accounts, douyin_store, douyin_timed
from app.tenancy import tenant_scope
from tests.conftest import T1

NOW = datetime(2026, 10, 20, 12, 25, 0)
ACCTS = [{"id": "yx-1", "status": 1}, {"id": "yx-2", "status": 1},
         {"id": "a3", "status": 0}]          # 停用的号不进派单


@pytest.fixture()
def env(monkeypatch):
    """把 store / 账号池 / 队列换成可控替身，返回 (清单, 入队记录)。"""
    state = {"book": {}}
    monkeypatch.setattr(douyin_store, "load_timed", lambda: dict(state["book"]))
    monkeypatch.setattr(douyin_store, "save_timed",
                        lambda d: state.update(book=dict(d)))
    monkeypatch.setattr(douyin_store, "list_packs",
                        lambda: [{"id": 1}, {"id": 2}, {"id": 3}])
    monkeypatch.setattr(douyin_store, "read_pack",
                        lambda pid: {"id": int(pid)} if int(pid) <= 3 else _missing(pid))
    monkeypatch.setattr(douyin_accounts, "list_accounts",
                        lambda cached=False: list(ACCTS))
    enq: list = []
    monkeypatch.setattr(jobs_mod, "enqueue",
                        lambda name, payload, **kw: enq.append((name, payload)))
    return state, enq


def _missing(pid):
    raise NotFound(f"内容包 #{pid} 不存在")


def _fire(now=NOW):
    return douyin_timed.fire("t1", now, [], [])


# ── 时间归一 ────────────────────────────────────────────────────────────────

def test_norm_at_accepts_common_shapes():
    assert douyin_timed.norm_at("2026-10-20 12:25") == "2026-10-20 12:25"
    assert douyin_timed.norm_at("2026-10-20 12:25:07") == "2026-10-20 12:25"
    assert douyin_timed.norm_at("2026/10/20 12:25") == "2026-10-20 12:25"
    assert douyin_timed.norm_at(" 2026-10-20T12:25 ") == "2026-10-20 12:25"
    assert douyin_timed.norm_at("") == "" and douyin_timed.norm_at(None) == ""


def test_norm_at_rejects_garbage():
    """认不出就是空 —— 由调用方（pack_save / 页面）如实报错，不猜一个时间。"""
    for bad in ("2026-10-20", "12:25", "明天十二点", "2026-13-40 12:25"):
        assert douyin_timed.norm_at(bad) == ""


def test_parse_at_is_aware():
    """必须 aware：tick 传的是 aware，naive 相减会 TypeError 被吞掉。
    （本文件 NOW 一律用 naive —— 顺手钉住"两侧都会归一"这件事。）"""
    dt = douyin_timed.parse_at("2026-10-20 12:25")
    assert dt.tzinfo is not None and dt.replace(tzinfo=None) == NOW
    assert douyin_timed.parse_at("坏值") is None


# ── 写入 / 读取清单 ─────────────────────────────────────────────────────────

def test_set_at_marks_pending_and_clears(env):
    state, _enq = env
    assert douyin_timed.set_at(1, "2026-10-20 12:25") == "2026-10-20 12:25"
    assert state["book"]["1"]["state"] == "pending"
    assert douyin_timed.scheduled_count() == 1
    assert douyin_timed.set_at(1, "") == ""          # 留空 = 取消定时
    assert state["book"] == {}


def test_set_at_unknown_pack_raises(env):
    with pytest.raises(NotFound):
        douyin_timed.set_at(99, "2026-10-20 12:25")


def test_decorate_attaches_schedule_fields(env):
    """页面拿到的每个包都带定时字段：没排期的给空串（页面据此不显示 ⏰）。"""
    packs = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]
    env[0]["book"] = {"1": {"at": "2026-10-20 12:25", "state": "fired",
                            "fired_at": "2026-10-20 12:25:07", "accounts": 2}}
    douyin_timed.decorate(packs)
    assert packs[0]["publish_at"] == "2026-10-20 12:25"
    assert packs[0]["publish_state"] == "fired" and packs[0]["publish_accounts"] == 2
    assert packs[1]["publish_at"] == "" and packs[1]["publish_state"] == ""


# ── 到点派单 ────────────────────────────────────────────────────────────────

def test_fire_before_due_does_nothing(env):
    state, enq = env
    state["book"] = {"1": {"at": "2026-10-20 13:00", "state": "pending"}}
    fired, _skipped = _fire()
    assert not fired and not enq


def test_fire_fans_out_to_all_live_accounts(env):
    """到点 → 一条 publish 任务，发到当前所有可用号（each）。"""
    state, enq = env
    state["book"] = {"1": {"at": "2026-10-20 12:25", "state": "pending"}}
    fired, _skipped = _fire()
    assert fired == ["t1:douyin:timed:1:2"]
    assert enq == [("douyin.publish.batch",
                    {"pack_ids": ["1"], "account_ids": ["yx-1", "yx-2"],
                     "mode": "each"})]
    ent = state["book"]["1"]
    assert ent["state"] == "fired" and ent["accounts"] == 2
    assert ent["fired_at"] == "2026-10-20 12:25:00"


def test_fire_is_one_shot(env):
    """发过就不再来一遍 —— 重复派单 = 同一篇图文在矩阵里铺两遍。"""
    state, enq = env
    state["book"] = {"1": {"at": "2026-10-20 12:25", "state": "pending"}}
    _fire()
    enq.clear()
    for minutes in (1, 5, 60, 24 * 60):
        fired, _s = _fire(NOW + timedelta(minutes=minutes))
        assert not fired and not enq


def test_fire_late_within_window_catches_up(env):
    """应用没开着 → 窗口内（≤3 小时）开机就补发，并在日志里点明迟到。"""
    state, enq = env
    state["book"] = {"1": {"at": "2026-10-20 10:00", "state": "pending"}}
    fired, _skipped = _fire()
    assert enq and len(fired) == 1 and "迟到" in fired[0]


def test_fire_too_late_marks_missed(env):
    """超过补发窗口：记 `missed` + 如实报原因，不偷偷发也不偷偷丢。"""
    state, enq = env
    state["book"] = {"1": {"at": "2026-10-19 12:25", "state": "pending"}}
    fired, skipped = _fire()
    assert not fired and not enq
    assert state["book"]["1"]["state"] == "missed"
    assert any("已错过" in s and "1" in s for s in skipped)


def test_fire_skips_when_no_live_accounts(env, monkeypatch):
    """一个能发的号都没有 → 不派单，保持 pending，下一轮再试（不丢这条作品）。"""
    state, enq = env
    state["book"] = {"1": {"at": "2026-10-20 12:25", "state": "pending"}}
    monkeypatch.setattr(douyin_accounts, "list_accounts", lambda cached=False: [])
    fired, skipped = _fire()
    assert not fired and not enq
    assert state["book"]["1"]["state"] == "pending"
    assert any("无可用账号" in s for s in skipped)


def test_fire_drops_orphan_and_broken_entries(env):
    """包被删了 / 时间写坏了 → 清掉这条孤儿定时，别留着每次 tick 都报警。"""
    state, _enq = env
    state["book"] = {"9": {"at": "2026-10-20 12:25", "state": "pending"},
                     "2": {"at": "坏值", "state": "pending"}}
    _fire()
    assert state["book"] == {}


def test_fire_one_per_tick(env):
    """积压（长时间没开机）时每分钟只派一条：不至于一次灌出几十条图文。"""
    state, enq = env
    state["book"] = {"1": {"at": "2026-10-20 12:00", "state": "pending"},
                     "2": {"at": "2026-10-20 12:10", "state": "pending"}}
    fired, skipped = _fire()
    assert len(enq) == 1 and len(fired) == 1
    assert state["book"]["1"]["state"] == "fired"
    assert state["book"]["2"]["state"] == "pending", "剩下的留到下一轮"
    assert any("已到点" in s for s in skipped)
    # 下一轮（+1 分钟）接着发第二条
    fired2, _s2 = _fire(NOW + timedelta(minutes=1))
    assert len(enq) == 2 and len(fired2) == 1 and state["book"]["2"]["state"] == "fired"


# ── 与调度线程的接线 ────────────────────────────────────────────────────────

def test_scheduler_helper_needs_app_context(app):
    """没有应用上下文时读不到租户目录 → 如实返回空，不抛异常拖垮整轮 tick。"""
    from app.services import scheduler

    assert scheduler._fire_douyin_timed("t1", NOW) == ([], [])


def test_scheduler_fires_from_real_disk(app, monkeypatch):
    """端到端：pack/save 写下的定时 → tick 真的派单（走真实 timed.json）。"""
    from app.services import scheduler

    enq: list = []
    monkeypatch.setattr(jobs_mod, "enqueue",
                        lambda name, payload, **kw: enq.append((name, payload)))
    monkeypatch.setattr(douyin_accounts, "list_accounts",
                        lambda cached=False: list(ACCTS))
    with app.app_context(), tenant_scope(T1):
        douyin_store.write_pack({"id": 1, "title": "首条", "images": []})
        douyin_timed.set_at(1, "2026-10-20 12:25")
        fired, _skipped = scheduler._fire_douyin_timed(T1, NOW)
        assert fired and enq[0][1]["pack_ids"] == ["1"]
        assert enq[0][1]["mode"] == "each"
        assert douyin_store.load_timed()["1"]["state"] == "fired"
        # 再 tick 一次：不再派单（一次性）
        enq.clear()
        scheduler._fire_douyin_timed(T1, NOW + timedelta(minutes=2))
        assert not enq