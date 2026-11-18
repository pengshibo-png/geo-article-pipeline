"""抖音图文「定时发布」的回归测试（2026-10-18 新增功能）。

设计：`autopub.mode = schedule` 时，调度器**每天在指定时刻**触发发布，
每档发 N 条；靠 `last_shot`（{时刻: 日期}）保证同一档当天只触发一次
（tick 每分钟都跑，不记就会触发几十次）。
"""
from __future__ import annotations

from datetime import datetime

from app.platform import jobs as jobs_mod


def test_schedule_normalization():
    """时段归一：非法时刻丢弃、全角冒号兼容、按时间排序、条数钳到 1–10。"""
    from app.services.douyin_schedule import norm_schedule as _norm_schedule

    out = _norm_schedule([
        {"time": "9:5", "count": 3},
        {"time": "25:00"},                  # 小时越界
        {"time": "abc"},                    # 不是时间
        {"time": "21：30", "count": 99},    # 全角冒号 + 超上限
        {"time": "09:00", "enabled": False},
    ])
    assert [s["time"] for s in out] == ["09:00", "09:05", "21:30"], "必须按时间排序"
    assert out[0]["count"] == 1 and out[0]["enabled"] is False
    assert out[2]["count"] == 10, "条数要钳到 10"


def test_schedule_fires_once_per_slot_per_day(app, monkeypatch):
    """到点触发一次；同一天同一档再 tick 不能重复触发（防刷屏）。"""
    from app.services import douyin_store, scheduler

    cfg = {"enabled": True, "mode": "schedule",
           "schedule": [{"time": "09:00", "count": 2, "enabled": True}],
           "account_ids": ["a1"], "order": "old",
           "auto_gen": False, "gen_mode": "off", "last_shot": {}}
    saved: dict = {}
    monkeypatch.setattr(douyin_store, "load_autopub", lambda: dict(cfg))
    monkeypatch.setattr(douyin_store, "save_autopub", lambda d: saved.update(d))
    monkeypatch.setattr(douyin_store, "load_ledger", lambda: {})
    monkeypatch.setattr(douyin_store, "list_packs",
                        lambda: [{"id": 1}, {"id": 2}, {"id": 3}])
    enq: list = []
    monkeypatch.setattr(jobs_mod, "enqueue",
                        lambda name, payload, **kw: enq.append((name, payload)))

    with app.app_context():
        fired, _ = scheduler._fire_douyin_autopub("t1", datetime(2026, 10, 18, 9, 0))

    assert fired and enq, "到点必须派发"
    assert enq[0][0] == "douyin.publish.batch"
    assert enq[0][1]["pack_ids"] == ["1", "2"], "按 count 取 N 条、顺序取旧包"
    assert saved.get("last_shot", {}).get("09:00") == "2026-10-18", "要记当天已触发"

    # 同一档、当天晚一分钟再 tick → 不再触发
    enq.clear()
    cfg["last_shot"] = saved["last_shot"]
    with app.app_context():
        fired2, _ = scheduler._fire_douyin_autopub("t1", datetime(2026, 10, 18, 9, 1))

    assert not fired2 and not enq, "同一档当天只能触发一次"

    # 第二天同一时刻 → 又会触发
    enq.clear()
    with app.app_context():
        fired3, _ = scheduler._fire_douyin_autopub("t1", datetime(2026, 10, 19, 9, 0))
    assert fired3 and enq, "次日同一时刻应再次触发"

    # tick 是 sleep(60) 循环、周期会漂移：迟到 12 分钟仍要补发
    enq.clear()
    cfg["last_shot"] = {}
    with app.app_context():
        fired4, _ = scheduler._fire_douyin_autopub("t1", datetime(2026, 10, 20, 9, 12))
    assert fired4 and enq, "15 分钟补发窗口内应补发"

    # 迟到太久（40 分钟）不补 —— 否则半夜启动会突然开始发稿
    enq.clear()
    cfg["last_shot"] = {}
    with app.app_context():
        fired5, _ = scheduler._fire_douyin_autopub("t1", datetime(2026, 10, 20, 9, 40))
    assert not fired5 and not enq, "超出窗口不该补发"


def _mock_fire_env(app, monkeypatch, slot_extra: dict):
    """把 autopub 配置与 douyin_store/jobs 都换成可控替身，返回 (cfg, enq)。"""
    from app.services import douyin_store

    slot = {"time": "09:00", "count": 1, "enabled": True}
    slot.update(slot_extra)
    cfg = {"enabled": True, "mode": "schedule", "schedule": [slot],
           "account_ids": ["a1"], "order": "old",
           "auto_gen": False, "gen_mode": "off", "last_shot": {}}
    saved: dict = {}
    monkeypatch.setattr(douyin_store, "load_autopub", lambda: dict(cfg))
    monkeypatch.setattr(douyin_store, "save_autopub", lambda d: saved.update(d))
    monkeypatch.setattr(douyin_store, "load_ledger", lambda: {})
    monkeypatch.setattr(douyin_store, "list_packs",
                        lambda: [{"id": 1}, {"id": 2}, {"id": 3}])
    enq: list = []
    monkeypatch.setattr(jobs_mod, "enqueue",
                        lambda name, payload, **kw: enq.append((name, payload)))
    return cfg, enq, saved


def test_schedule_normalization_keeps_days_and_accounts():
    """时段表还要保住 days（0–6）与 account_ids。"""
    from app.services.douyin_schedule import norm_schedule

    out = norm_schedule([{"time": "09:00", "count": 1,
                          "days": [4, 0, 0, 9, "1"], "account_ids": ["a1", 2]}])
    assert out[0]["days"] == [0, 1, 4], "星期 0–6 去重排序，越界丢弃"
    assert out[0]["account_ids"] == ["a1", "2"]


def test_schedule_filters_by_weekday(app, monkeypatch):
    """配了"只工作日发" → 周日不触发、周一触发。"""
    from datetime import datetime

    from app.services import scheduler

    _cfg, enq, _saved = _mock_fire_env(app, monkeypatch, {"days": [0, 1, 2, 3, 4]})

    with app.app_context():          # 2026-10-18 是周日
        fired, _ = scheduler._fire_douyin_autopub("t1", datetime(2026, 10, 18, 9, 0))
    assert not fired and not enq, "周日不该触发"

    with app.app_context():          # 2026-10-19 是周一
        fired2, _ = scheduler._fire_douyin_autopub("t1", datetime(2026, 10, 19, 9, 0))
    assert fired2 and enq, "周一应触发"


def test_schedule_slot_can_override_accounts(app, monkeypatch):
    """每档指定账号 → 用这一档的号，而不是卡片里勾选的那批。"""
    from datetime import datetime

    from app.services import scheduler

    _cfg, enq, _saved = _mock_fire_env(app, monkeypatch,
                                       {"days": [], "account_ids": ["b9"]})
    with app.app_context():
        scheduler._fire_douyin_autopub("t1", datetime(2026, 10, 19, 9, 0))
    assert enq and enq[0][1]["account_ids"] == ["b9"], "应使用本档指定的账号"


def test_schedule_requires_enabled_slot_when_saving(app):
    """开了定时发布却没有任何启用时刻 → 拒绝保存（否则等于永远不发）。"""
    import pytest

    from app.blueprints.generation.douyin_service import DouyinService
    from app.errors import ValidationError
    from app.tenancy import tenant_scope

    with app.app_context(), tenant_scope("t_alpha"), pytest.raises(ValidationError):
        DouyinService().autopub_save({
            "enabled": True, "mode": "schedule",
            "schedule": [{"time": "09:00", "count": 1, "enabled": False}],
            "account_ids": ["a1"],
        })
