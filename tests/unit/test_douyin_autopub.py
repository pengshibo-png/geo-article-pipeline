"""抖音图文「循环发布」的回归测试（2026-10-19 复查出的三项）。

1. 「保存并立即发一篇」在 interval 模式下无效 —— force 只被 schedule 分支读了；
2. `gen_every_min` / `gen_mode=interval` 是**死配置**（页面上有开关，没有任何代码读）；
3. 倒计时 `next_in_sec` 后端从来没返回过 → 页面渲染成 `NaN:NaN`。
"""
from __future__ import annotations

from datetime import datetime

from app.platform import jobs as jobs_mod
from app.services.douyin_schedule import next_in_sec


# ── 倒计时 ──────────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 19, 8, 0, 0)


def test_next_in_sec_none_when_disabled():
    assert next_in_sec({"enabled": False}, NOW) is None
    assert next_in_sec({}, NOW) is None


def test_next_in_sec_interval_counts_down():
    """循环模式：剩余秒数 = 间隔 - 已过时间（页面倒计时的真实来源）。"""
    cfg = {"enabled": True, "mode": "interval", "interval_min": 30,
           "last_run": "2026-10-19 07:50:00"}
    assert next_in_sec(cfg, NOW) == 20 * 60


def test_next_in_sec_interval_never_negative():
    """早就该发了（或时钟回拨）→ 0，不能给负数让页面显示 `-1:-5`。"""
    cfg = {"enabled": True, "mode": "interval", "interval_min": 30,
           "last_run": "2026-10-19 05:00:00"}
    assert next_in_sec(cfg, NOW) == 0


def test_next_in_sec_interval_never_ran():
    """首次启用还没跑过 → 0（下一页 tick 就发），而不是空值。"""
    cfg = {"enabled": True, "mode": "interval", "interval_min": 30, "last_run": ""}
    assert next_in_sec(cfg, NOW) == 0


def test_next_in_sec_schedule_uses_next_slot():
    cfg = {"enabled": True, "mode": "schedule",
           "schedule": [{"time": "09:00", "enabled": True},
                        {"time": "12:00", "enabled": True}]}
    assert next_in_sec(cfg, NOW) == 60 * 60, "应指向最近的一档 09:00"


def test_next_in_sec_schedule_respects_days():
    """配了"只在周一发"：周日算出来的应是**次日**（周一）那一档。"""
    sunday = datetime(2026, 10, 18, 8, 0, 0)
    cfg = {"enabled": True, "mode": "schedule",
           "schedule": [{"time": "09:00", "enabled": True, "days": [0]}]}
    assert next_in_sec(cfg, sunday) == 25 * 60 * 60


def test_next_in_sec_schedule_without_enabled_slot_is_none():
    """没有启用的档位 → None（页面据此显示"待定"，不再显示乱码）。"""
    cfg = {"enabled": True, "mode": "schedule",
           "schedule": [{"time": "09:00", "enabled": False}]}
    assert next_in_sec(cfg, NOW) is None


# ── force（「保存并立即发一篇」）──────────────────────────────────────────────

def _env(app, monkeypatch, **cfg_over):
    """把 autopub 配置与 douyin_store/jobs 换成可控替身。"""
    from app.services import douyin_store

    cfg = {"enabled": True, "mode": "interval", "interval_min": 30,
           "account_ids": ["a1"], "order": "old", "auto_gen": True,
           "gen_mode": "stock", "gen_every_min": 60, "gen_batch": 2,
           "last_run": "2026-10-19 07:59:00",       # 刚发过 → 不到点
           "last_gen": "", "last_shot": {}}
    cfg.update(cfg_over)
    saved: dict = {}
    # 读回的必须是**上次落盘的那份**（否则测不出 last_run / last_gen 的防重效果）
    monkeypatch.setattr(douyin_store, "load_autopub", lambda: dict(saved or cfg))
    monkeypatch.setattr(douyin_store, "save_autopub", lambda d: saved.update(d))
    monkeypatch.setattr(douyin_store, "load_ledger", lambda: {})
    monkeypatch.setattr(douyin_store, "list_packs",
                        lambda: [{"id": 1}, {"id": 2}, {"id": 3}])
    enq: list = []
    monkeypatch.setattr(jobs_mod, "enqueue",
                        lambda name, payload, **kw: enq.append((name, payload)))
    return cfg, enq, saved


def test_interval_force_publishes_even_when_not_due(app, monkeypatch):
    """「保存并立即发一篇」必须真的发 —— 旧实现只在 schedule 分支读 force，
    interval 模式下按钮点了等于"只保存"。"""
    from app.services import scheduler

    _cfg, enq, saved = _env(app, monkeypatch)
    with app.app_context():
        fired, _ = scheduler._fire_douyin_autopub("t1", NOW, force=True)
    assert fired, "force 时必须派发"
    assert enq[0][0] == "douyin.publish.batch" and enq[0][1]["pack_ids"] == ["1"]
    assert saved.get("last_run") == "2026-10-19 08:00:00", "要重置计时，不能立刻再发一篇"


def test_interval_not_due_does_nothing(app, monkeypatch):
    """没到点（且未 force）→ 一条都不发，避免 tick 每分钟刷屏。"""
    from app.services import scheduler

    _cfg, enq, _saved = _env(app, monkeypatch)
    with app.app_context():
        fired, _ = scheduler._fire_douyin_autopub("t1", NOW)
    assert not fired and not enq


def test_interval_refills_on_gen_every_min(app, monkeypatch):
    """`gen_mode=interval` 到点就补货（库存还满也补），并记 last_gen 防连环补。"""
    from app.services import scheduler

    _cfg, enq, saved = _env(app, monkeypatch, gen_mode="interval", gen_every_min=60,
                            last_run="2026-10-19 05:00:00")
    with app.app_context():
        scheduler._fire_douyin_autopub("t1", NOW)
    assert [e[0] for e in enq] == ["douyin.publish.batch", "douyin.generate.batch"]
    assert saved.get("last_gen") == "2026-10-19 08:00:00"

    # 下一个发布点到（08:30）再 tick：补货未到点（间隔 60 分钟）→ 只发布，不再补
    enq.clear()
    with app.app_context():
        scheduler._fire_douyin_autopub("t1", datetime(2026, 10, 19, 8, 30, 0))
    assert [e[0] for e in enq] == ["douyin.publish.batch"], "60 分钟内不该补第二次"


def test_interval_stock_mode_refills_only_when_empty(app, monkeypatch):
    """`gen_mode=stock`：库存够就不补（页面写的是"库存不足自动生成"）。"""
    from app.services import scheduler

    _cfg, enq, _saved = _env(app, monkeypatch, gen_mode="stock",
                             last_run="2026-10-19 05:00:00")
    with app.app_context():
        scheduler._fire_douyin_autopub("t1", NOW)
    assert [e[0] for e in enq] == ["douyin.publish.batch"]


def test_interval_skips_without_accounts(app, monkeypatch):
    """没勾号 → 一条都不发，并如实记 skip（不是静默不动）。"""
    from app.services import scheduler

    _cfg, enq, _saved = _env(app, monkeypatch, account_ids=[],
                             last_run="2026-10-19 05:00:00")
    with app.app_context():
        fired, skipped = scheduler._fire_douyin_autopub("t1", NOW)
    assert not fired and not enq and skipped