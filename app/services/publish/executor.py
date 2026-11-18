"""发布执行器：PublishTask 状态机（queued → running → success | failed）。

幂等（ADR-011 延伸）：**重复发布 = 事故**。idem_key = hash(tenant, dir, account, day)，
同文章同账号同自然日只发一次；`enqueue_publish` 命中已存在的当天任务直接复用。
额度扣减走 `Account.quota_update_sql()` 单条 UPDATE（跨日自动重置，无超发）。

任务体在 worker 线程执行（run_job 已绑租户 + 统一 commit/rollback），
每次发布无论成败都落一条 PublishLog（append-only 事实记录，ADR 问题 8）。
"""
from __future__ import annotations

import hashlib
import threading

from app.logging import get_logger
from app.models import Account, Article, PublishLog, PublishTask
from app.platform import storage
from app.platform.jobs import task
from app.platform.state_bridge import bridge
from app.services.publish.drivers import PublishError, classify_error, get_driver
from app.tenancy import TenantScopedRepository, current_tenant
from app.timeutil import natural_day_string

log = get_logger(__name__)


def make_idem_key(article_dir: str, account_id: str, day: str) -> str:
    raw = f"{current_tenant()}|{article_dir}|{account_id}|{day}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def enqueue_publish(article_dir: str, account: Account, *, session=None) -> PublishTask:
    """建任务（或复用当天已发的）并入队 publish 队列。

    **先 commit 再入队**：worker 线程是另一条 SQLite 连接，读不到未提交行；
    顺序反了会出现"任务行还没可见，worker 已开跑"的竞态。
    """
    from app.platform import jobs

    day = natural_day_string()
    idem = make_idem_key(article_dir, account.id, day)
    repo = TenantScopedRepository(PublishTask)
    existing = repo.query().filter(PublishTask.idem_key == idem).first()
    if existing is not None:
        # 今天已成功发过 → 原样复用（幂等：重复发布 = 事故）。
        # 上次失败/被取消 → 复位重投：否则用户点"发布"只会读到旧错误，
        # 当天没有任何重试入口（队列里也不会再出现这个任务）。
        if existing.status in ("failed", "canceled", "dead"):
            existing.status = "queued"
            existing.error = None
            existing.error_code = None
            existing.finished_at = None
            existing.queued_at = _now()
            (session or _default_session()).commit()
            jobs.enqueue("publish.run", {"task_id": existing.id, "idem_key": idem},
                         tenant_id=current_tenant(), queue="publish")
        return existing

    row = PublishTask(article_dir=article_dir, account_id=account.id, platform=account.platform,
                      status="queued", idem_key=idem, queued_at=_now())
    repo.add(row)
    (session or _default_session()).commit()
    jobs.enqueue("publish.run", {"task_id": row.id, "idem_key": idem},
                 tenant_id=current_tenant(), queue="publish")
    return row


def _default_session():
    from app.extensions import db
    return db.session


def _now():
    from app.timeutil import now_local
    return now_local()


def _settle(task_row: PublishTask, *, ok: bool, url: str = "", code: str = "", err: str = "") -> None:
    task_row.status = "success" if ok else "failed"
    task_row.url = url or None
    task_row.error_code = None if ok else (code or "unknown")
    task_row.error = None if ok else err
    task_row.finished_at = _now()
    art = TenantScopedRepository(Article).query().filter(
        Article.dir == task_row.article_dir).first()
    repo = TenantScopedRepository(PublishLog)
    repo.add(PublishLog(
        task_id=task_row.id, article_dir=task_row.article_dir,
        title=(art.title if art else None) or task_row.article_dir,
        account_id=task_row.account_id, platform=task_row.platform, mode=task_row.mode,
        success=ok, error=None if ok else err, url=url or None, happened_at=_now(),
    ))


@task("publish.run")
def publish_run_job(payload: dict) -> dict:
    """执行一条发布任务（按 idem_key 定位，入队时任务行可能尚未 commit）。

    任务中心批次旗标（§1.5 规则 10 + tasks.html 暂停/继续）：stop 置位后
    排队中的任务不再起跑，直接落 canceled —— 页面把非 success 当失败显示，
    canceled 语义通过 error 文案「已取消」传达。
    """
    tid = current_tenant()
    repo = TenantScopedRepository(PublishTask)
    row = None
    if payload.get("task_id"):
        row = repo.query().filter(PublishTask.id == payload["task_id"]).first()
    if row is None and payload.get("idem_key"):
        row = repo.query().filter(PublishTask.idem_key == payload["idem_key"]).first()
    if row is None:
        raise ValueError("publish.run 找不到任务")
    if row.status != "queued":
        return {"task_id": row.id, "status": row.status, "skipped": True}

    if should_stop(tid):
        _settle(row, ok=False, code="unknown", err="已取消（任务中心收到暂停指令）")
        row.status = "canceled"
        task_log(tid, f"⏸ 「{row.article_dir}」已取消")
        return {"task_id": row.id, "status": "canceled"}

    task_log(tid, f"▶ 开始发布「{row.article_dir}」→ {row.platform}")
    acct_repo = TenantScopedRepository(Account)
    account = acct_repo.query().filter(Account.id == row.account_id).first()
    if account is None:
        row.attempts += 1
        _settle(row, ok=False, code="login_expired", err="账号不存在或未授权")
        task_log(tid, f"❌ 「{row.article_dir}」失败：账号不存在或未授权")
        return {"task_id": row.id, "status": "failed", "error_code": "login_expired"}

    row.status = "running"
    row.attempts += 1
    row.started_at = _now()

    # 原子扣额度（单条 UPDATE：跨日重置 + 校验一步完成，rowcount=0 即额度满）
    from app.extensions import db as _db
    from sqlalchemy import text

    day = natural_day_string()
    result = _db.session.execute(text(Account.quota_update_sql()),
                                 {"id": account.id, "tenant_id": current_tenant(), "today": day})
    if result.rowcount == 0:
        _settle(row, ok=False, code="quota", err="今日发布额度已用完，顺延下一时段")
        task_log(tid, f"❌ 「{row.article_dir}」失败：额度已用完")
        return {"task_id": row.id, "status": "failed", "error_code": "quota"}

    try:
        html = storage.read_article_file(row.article_dir, storage.FINAL)
    except Exception as exc:
        _settle(row, ok=False, code="unknown", err=f"终稿不可读：{exc}")
        task_log(tid, f"❌ 「{row.article_dir}」失败：终稿不可读")
        return {"task_id": row.id, "status": "failed", "error_code": "unknown"}

    # ★ 发布前最后一道手机号闸：不管稿子怎么来的（keep 模式老稿 / 降级交付 /
    # 回退初稿 / 人工改过），只要正文里还有手机号就清掉再发。只删号码不动别的，
    # 也不改写磁盘文件（用户看到的稿子与发出去的内容在这一点上保持一致）。
    from app.services.writer.compliance import strip_mobile

    html, mob_hits = strip_mobile(html)
    if mob_hits:
        task_log(tid, f"🧹 「{row.article_dir}」发布前清除手机号 {len(mob_hits)} 处")
        log.warning("publish_mobile_stripped", extra={
            "event": "publish_mobile_stripped", "dir": row.article_dir,
            "hits": len(mob_hits)})

    art_row = TenantScopedRepository(Article).query().filter(
        Article.dir == row.article_dir).first()
    try:
        # 驱动选择：绑定了蚁小二云账号 → 走蚁小二一键分发；否则走平台专用驱动
        # （平台驱动自带"有 Cookie 才真发"的判定，离线时 note 会在日志里说明）
        driver = _yxer_driver(account) or get_driver(row.platform, account)
        if getattr(driver, "note", ""):
            task_log(tid, f"ℹ {driver.note}")
        url = driver.publish(html=html, title=platform_title(row.article_dir, art_row),
                             account=account)
    except PublishError as exc:
        _settle(row, ok=False, code=exc.code, err=exc.message)
        _mark_account_on_failure(account, exc)
        task_log(tid, f"❌ 「{row.article_dir}」失败：{exc.message}")
        return {"task_id": row.id, "status": "failed", "error_code": exc.code}
    except Exception as exc:  # 驱动侧未归类的异常
        code = classify_error(str(exc))
        _settle(row, ok=False, code=code, err=str(exc))
        task_log(tid, f"❌ 「{row.article_dir}」失败：{exc}")
        return {"task_id": row.id, "status": "failed", "error_code": code}

    _settle(row, ok=True, url=url)
    art = TenantScopedRepository(Article).query().filter(Article.dir == row.article_dir).first()
    if art is not None:
        art.status = "published"
    task_log(tid, f"✅ 「{row.article_dir}」发布成功 {url}")
    log.info("published", extra={"event": "published", "task_id": row.id, "platform": row.platform})
    return {"task_id": row.id, "status": "success", "url": url}


def platform_title(article_dir: str, article=None) -> str:
    """发布到平台的标题：优先用文章真标题（库里的 title），退化到目录名。"""
    text = (getattr(article, "title", None) or article_dir or "").strip()
    return text[:64] or article_dir


def _yxer_driver(account):
    """账号绑定过蚁小二云账号（yxer_id 非空）→ 返回 YxerDriver 实例。"""
    if not getattr(account, "yxer_id", None):
        return None
    from app.services.publish.yxer import YxerDriver
    return YxerDriver()


def _mark_account_on_failure(account: Account, exc: PublishError) -> None:
    """限流/熔断 → 冻结渠道稍后重试；登录失效 → 标记 invalid 等重新授权（§1.5 规则 2/3）。"""
    if exc.code in ("rate_limited",):
        account.status = "frozen"
    elif exc.code == "login_expired":
        account.status = "invalid"


# ── 任务中心（tasks.html）批次控制：内存旗标 + 日志尾（与 managed 同一约定）──

_TASK_LOCK = threading.Lock()
# tid → {"stop": bool, "log": [ts 前缀行, ...]}（cap 120 行，页面 renderLogTail 读尾部）
# **有界容器**（StateBridge）：超上限驱逐最久未访问的租户。模块级裸 dict 每租户
# 一条、永不回收，长驻进程下是内存泄漏（state_bridge 那批改造漏掉了这个模块）。
_TASK_FLAGS = bridge(lambda: {"stop": False, "log": []})


def _task_state(tid: str) -> dict:
    return _TASK_FLAGS.get(tid)


def task_log(tid: str, line: str) -> None:
    """任务中心日志必须带 [HH:MM:SS] 前缀（页面按正则提取时间戳）。"""
    from datetime import datetime

    st = _task_state(tid)
    with _TASK_LOCK:
        st["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}")
        del st["log"][:-120]


def request_stop(tid: str) -> None:
    _task_state(tid)["stop"] = True


def clear_stop(tid: str) -> None:
    _task_state(tid)["stop"] = False


def should_stop(tid: str) -> bool:
    return bool(_task_state(tid)["stop"])


def log_tail(tid: str, n: int = 25) -> list:
    st = _task_state(tid)
    with _TASK_LOCK:
        return list(st["log"][-n:])


def reset_task_flags() -> None:
    """测试隔离：清空任务中心旗标/日志（进程内全局态）。"""
    with _TASK_LOCK:
        _TASK_FLAGS.clear()


__all__ = ["enqueue_publish", "publish_run_job", "make_idem_key",
           "request_stop", "clear_stop", "should_stop", "task_log", "log_tail",
           "reset_task_flags"]
