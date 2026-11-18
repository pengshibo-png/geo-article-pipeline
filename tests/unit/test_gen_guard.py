"""批量写稿状态桥的守卫测试（2026-10-22）。

覆盖三件事，都是实际踩过/审查出来的故障：
1. 原子抢占：连点两次只有一个能起（否则同一批选题被双跑、双份计费）；
2. 僵尸租约：批次 30 分钟无活动允许被强制抢占（否则页面永久"已有任务在运行"）；
3. gen_abort：启动阶段失败必须复位 running（否则只能重启进程）。
"""
from __future__ import annotations

import time

from app.services.writer import run_bridge


def _reset() -> None:
    run_bridge.reset_states()


def test_try_begin_is_atomic():
    _reset()
    tid = "t_guard"
    assert run_bridge.gen_try_begin(tid) is True
    assert run_bridge.gen_try_begin(tid) is False, "第二次必须被拒（防双跑）"
    assert run_bridge.gen_running(tid) is True


def test_abort_releases_running():
    _reset()
    tid = "t_guard"
    run_bridge.gen_try_begin(tid)
    run_bridge.gen_abort(tid, "入队失败")
    assert run_bridge.gen_running(tid) is False
    assert "入队失败" in str(run_bridge._gen_state(tid).get("error") or "")
    # 复位后可以重新起批次
    assert run_bridge.gen_try_begin(tid) is True


def test_zombie_lease_can_be_taken_over():
    _reset()
    tid = "t_guard"
    run_bridge.gen_try_begin(tid)
    st = run_bridge._gen_state(tid)
    # 模拟"任务丢了、running 卡住很久"
    st["last_activity"] = time.time() - run_bridge._ZOMBIE_SEC - 60
    assert run_bridge.gen_try_begin(tid) is True, "超租约应允许强制抢占"


def test_set_total_does_not_reset_counters():
    _reset()
    tid = "t_guard"
    run_bridge.gen_try_begin(tid)
    run_bridge.gen_set_total(tid, 3)
    prog = run_bridge.gen_snapshot(tid)["progress"]
    assert prog["total"] == 3
    assert prog["done"] == 0


def test_finish_article_releases_when_done():
    _reset()
    tid = "t_guard"
    run_bridge.gen_try_begin(tid)
    run_bridge.gen_set_total(tid, 1)
    run_bridge.gen_finish_article(tid, "标题", ok=True)
    assert run_bridge.gen_running(tid) is False
    assert run_bridge.gen_snapshot(tid)["progress"]["success"] == 1
