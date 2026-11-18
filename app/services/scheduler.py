"""定时计划执行器：/api/autosched 的落地引擎（title/article/publish 三类计划）。

调度模型（单进程版，与 ADR-004 阶段一一致）：
- 一个 daemon 线程每 interval 秒调一次 `tick()`；
- `tick()` 遍历所有 active 租户（调度是系统级职责，允许跨租户循环，
  每个租户的动作在 `tenant_scope()` 内执行，写守卫照常拦截越权）；
- Schedule 行 enabled 且 `next_run_at` 到期 → 触发对应任务 → 推进 next_run_at。
  首次遇到（next_run_at 为空）只初始化到下一个时点，**不立即执行**
  （保存计划≠马上跑，与原站行为一致）。

触发语义：
- title  → titles.generate.managed（count=计划条数；该租户选题任务在跑则跳过本期）
- article→ managed.begin_run（与 /api/run 同一入口；运行中/品牌缺失本期跳过）
- publish→ config JSON 里的每账号计划：各账号发 `count` 篇未发布终稿（幂等键防重）

桌面版由 desktop/launcher 启动线程；测试直接调 tick() 断言"到期才触发"。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

from app.extensions import db
from app.logging import get_logger
from app.models import Account, Article, Schedule, Tenant
from app.platform import jobs
from app.tenancy import TenantScopedRepository, tenant_scope
from app.timeutil import now_local, shanghai

log = get_logger(__name__)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=shanghai())


def _parse_at(text, fallback_hour: int, fallback_min: int):
    try:
        hh, mm = str(text or "").split(":")
        return int(hh), int(mm)
    except ValueError:
        return fallback_hour, fallback_min


def _first_due(s: Schedule, now: datetime) -> datetime:
    """next_run_at 缺失时初始化（不立即执行）。"""
    if s.mode == "interval":
        return now + timedelta(minutes=int(s.every_min or 60))
    hh, mm = _parse_at(s.at_time, 3, 0)
    due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return due + timedelta(days=1) if due <= now else due


def _advance(s: Schedule, now: datetime) -> datetime:
    if s.mode == "interval":
        return now + timedelta(minutes=int(s.every_min or 60))
    hh, mm = _parse_at(s.at_time, 3, 0)
    base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return base + timedelta(days=1)


def tick(now: datetime | None = None) -> dict:
    """单轮调度扫描；返回 {fired: [...], skipped: [...]}（供测试与日志）。"""
    from app.services.publish import executor, status_sync
    from app.services.writer import managed

    # 调用方可能显式传 naive datetime（旧测试习惯），两侧都归一成 aware，
    # 否则 `_aware(s.next_run_at) > now` 会抛 TypeError 被 except 吞掉。
    now = _aware(now) if now is not None else now_local()
    fired, skipped = [], []
    tenants = db.session.query(Tenant).filter(Tenant.status == "active").all()
    for t in tenants:
        with tenant_scope(t.id):
            # 蚁小二异步发布：节流回查最近提交的结果（提交 ≠ 已发布）
            status_sync.sync_if_due(t.id)
            repo = TenantScopedRepository(Schedule)
            for s in repo.all():
                if not s.enabled:
                    continue
                if s.next_run_at is None:
                    s.next_run_at = _first_due(s, now)
                    continue
                if _aware(s.next_run_at) > now:
                    continue
                try:
                    if s.kind == "title":
                        if managed.titles_running(t.id):
                            skipped.append(f"{t.id}:title:busy")
                        else:
                            managed.titles_begin(t.id)
                            jobs.enqueue("titles.generate.managed",
                                         {"count": int(s.count or 50)},
                                         tenant_id=t.id, queue="gen")
                            fired.append(f"{t.id}:title")
                    elif s.kind == "article":
                        out = managed.begin_run(int(s.count or 30), "revise")
                        (fired if out.get("ok") else skipped).append(
                            f"{t.id}:article:{out.get('error', '')[:24]}")
                    elif s.kind == "publish":
                        n = _fire_publish_plans(t.id, executor)
                        fired.append(f"{t.id}:publish:{n}")
                except Exception as exc:  # noqa: BLE001 - 单个计划失败不拖垮整轮
                    log.warning("schedule_fire_failed", extra={
                        "event": "schedule_fire_failed", "tenant": t.id,
                        "kind": s.kind, "err": str(exc)})
                    skipped.append(f"{t.id}:{s.kind}:error")
                s.next_run_at = _advance(s, now)
            # 视频产线自动发布（与上面 Schedule 解耦：文件式 autopub.json，不进
            # Schedule 表）。不在 services 层 import blueprints（A2 守卫），只走
            # jobs.enqueue + video_store，任务体已在 video_jobs 注册。
            try:
                fv, sv = _fire_video_autopub(t.id, now)
                fired.extend(fv)
                skipped.extend(sv)
            except Exception as exc:  # noqa: BLE001
                log.warning("video_autopub_failed", extra={
                    "event": "video_autopub_failed", "tenant": t.id, "err": str(exc)})
                skipped.append(f"{t.id}:video:error")
            try:
                fd, sd = _fire_douyin_autopub(t.id, now)
                fired.extend(fd)
                skipped.extend(sd)
            except Exception as exc:  # noqa: BLE001
                log.warning("douyin_autopub_failed", extra={
                    "event": "douyin_autopub_failed", "tenant": t.id, "err": str(exc)})
                skipped.append(f"{t.id}:douyin:error")
            # 定时作品（每包一次性）：与上面的循环发布各走各的配置，互不影响
            try:
                ft, st_t = _fire_douyin_timed(t.id, now)
                fired.extend(ft)
                skipped.extend(st_t)
            except Exception as exc:  # noqa: BLE001
                log.warning("douyin_timed_failed", extra={
                    "event": "douyin_timed_failed", "tenant": t.id, "err": str(exc)})
                skipped.append(f"{t.id}:douyin:timed:error")
        db.session.commit()
    return {"fired": fired, "skipped": skipped}


def _fire_video_autopub(tid: str, now: datetime) -> tuple[list[str], list[str]]:
    """视频自动发布：启用且到点 → 发一条未发布视频；库存空则自动补产一条。

    到期才触发（保存计划 ≠ 立即跑，与原站一致）。不 import blueprints（A2 守卫），
    故直接走 jobs.enqueue + video_store。抖音图文一侧已改为同样"真正执行"
    （见 douyin_schedule.fire_autopub）。
    """
    from app.services import video_store
    from flask import has_app_context

    fired, skipped = [], []
    if not has_app_context():
        return fired, skipped
    # 两侧都归一成 aware：生产传 now_local()（aware），测试/旧配置可能给 naive。
    # 只归一一边仍会 "can't subtract offset-naive and offset-aware"。
    now = _aware(now)
    cfg = video_store.load_autopub()
    if not cfg.get("enabled"):
        return fired, skipped
    interval = max(10, min(int(cfg.get("interval_min") or 120), 1440))
    last = cfg.get("last_run") or ""
    last_dt = None
    if last:
        try:
            # last_run 落盘是 naive 本地时间串，而 now 是 tz-aware —— 不归一会
            # TypeError: can't subtract offset-naive and offset-aware datetimes，
            # 且会被 tick 的 except 吞掉 → 自动发布"跑一次就永久失效"。
            last_dt = _aware(datetime.strptime(last, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            last_dt = None
    if last_dt is not None and (now - last_dt).total_seconds() < interval * 60:
        return fired, skipped
    vids = [v for v in video_store.list_videos() if not v.get("published")]
    if vids:
        jobs.enqueue("video.publish", {"id": str(vids[0]["id"])},
                     tenant_id=tid, queue="gen")
        fired.append(f"{tid}:video:publish:{vids[0]['id']}")
    else:
        jobs.enqueue("video.generate", {}, tenant_id=tid, queue="gen")
        fired.append(f"{tid}:video:generate")
    cfg["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
    video_store.save_autopub(cfg)
    return fired, skipped


def _fire_douyin_autopub(tid: str, now: datetime,
                         force: bool = False) -> tuple[list[str], list[str]]:
    """抖音图文自动发布：全权交给 `douyin_schedule.fire_autopub`。

    这里只做两件事：应用上下文守卫 + naive/aware 归一。原实现在本文件里就地
    实现了 interval 模式（只认间隔、不认 `force`、也不认 `gen_every_min`），
    三个缺陷都在那段里 —— 整块搬进 `douyin_schedule`，与配置归一同一模块，
    顺带给本文件腾出行数（A3 单文件 300 行预算）。

    不 import blueprints（A2 守卫）：只走 jobs.enqueue + douyin_store，任务体
    douyin.publish.batch / douyin.generate.batch 已由应用工厂注册。
    """
    from flask import has_app_context

    from app.services import douyin_schedule

    fired, skipped = [], []
    if not has_app_context():
        return fired, skipped
    # 两侧都归一成 aware：生产传 now_local()（aware），测试/旧配置可能给 naive。
    # 只归一一边仍会 "can't subtract offset-naive and offset-aware"。
    now = _aware(now)
    return douyin_schedule.fire_autopub(tid, now, fired, skipped, force)


def _fire_douyin_timed(tid: str, now: datetime) -> tuple[list[str], list[str]]:
    """抖音图文「每条作品自己的发布时间」（douyin/timed.json，与 autopub 解耦）。

    与 _fire_douyin_autopub 同样的两条约定：应用上下文守卫 + naive/aware 归一。
    逻辑全在 `douyin_timed`（本文件只留这一小段，A3 单文件 300 行预算）。
    """
    from flask import has_app_context

    from app.services import douyin_timed

    fired, skipped = [], []
    if not has_app_context():
        return fired, skipped
    return douyin_timed.fire(tid, _aware(now), fired, skipped)


def _fire_publish_plans(tid: str, executor) -> int:
    """publish 计划（config JSON 数组）：每账号发 count 篇未发布终稿。"""
    s = TenantScopedRepository(Schedule).first(kind="publish")
    if s is None or not s.enabled or not (s.config or "").strip():
        return 0
    try:
        plans = json.loads(s.config)
    except ValueError:
        return 0
    queued = 0
    accounts = {a.id: a for a in TenantScopedRepository(Account).all()}
    for p in plans if isinstance(plans, list) else []:
        if not isinstance(p, dict) or not p.get("enabled"):
            continue
        account = accounts.get(str(p.get("account_id") or ""))
        if account is None or not account.enabled or account.status != "active" \
                or int(account.daily_limit) <= 0:
            continue
        want = max(1, min(int(p.get("count") or 1), 50))
        finals = (TenantScopedRepository(Article).query()
                  .filter(Article.status == "final")
                  .order_by(Article.created_at.asc()).limit(want).all())
        for a in finals:
            try:
                executor.enqueue_publish(a.dir, account)
                queued += 1
            except Exception:  # noqa: BLE001 - 无终稿/幂等冲突等单篇跳过
                log.warning("publish_plan_skip", extra={
                    "event": "publish_plan_skip", "tenant": tid, "dir": a.dir})
    return queued


def start(app, interval: int = 60) -> threading.Thread:
    """daemon 调度线程（桌面/单实例部署入口）。interval 秒一轮。"""

    def _loop():
        while True:
            try:
                with app.app_context():
                    tick()
            except Exception:  # noqa: BLE001 - 调度线程永不因单轮异常退出
                log.warning("scheduler_tick_failed", exc_info=True)
            time.sleep(interval)

    th = threading.Thread(target=_loop, daemon=True, name="geo-scheduler")
    th.start()
    return th
