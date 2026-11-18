"""distribution 蓝图 · 任务中心（tasks.html 对接面）。

契约（参考系统线上行为实测）：
- GET  /api/tasks → {running, current, tasks:[{id,title,platform,status,url,error,
                       account_name,created,finished}], log_tail}
- POST /api/client/queue-publish {dirs, accounts?} → {ok, queued}
- POST /api/tasks/stop|run → {ok}（暂停=不再起跑新任务；继续=清旗标重投排队项）
- GET  /api/client/preflight → {checking:false, channels:[{id,name,platform,ok}]}
- GET  /api/publish-pick → {ok, account_id}（智能分配：今日余量最少者优先，轮换）
- POST /api/publish-one {dir, account_id} → {ok, url?}（页面读同步结果）

状态映射：库内 `queued` → 页面 `pending`；`canceled` → 页面 `failed`（原因见 error）。
§1.5 规则 10：accounts 非空 = 每篇发所有勾选渠道；accounts=[] = 智能分配每篇一个账号。
虚拟聚合渠道（`*-all`）在队列里展开为其平台全部真实账号。
"""
from __future__ import annotations

import time

from app.errors import ValidationError
from app.extensions import db as _db
from app.logging import get_logger
from app.models import Account, Article, PublishTask
from app.platform import storage
from app.services.publish import executor
from app.timeutil import hm_string, natural_day_string
from app.tenancy import TenantScopedRepository, current_tenant

log = get_logger(__name__)

_STATUS_TO_PAGE = {"queued": "pending", "running": "running", "success": "success",
                   "failed": "failed", "canceled": "failed", "dead": "failed"}
_WAIT_S = 30.0


def _publishable(accounts) -> list:
    """可发布**文章**的账号：启用 + 活跃 + 有额度 + **平台能发文章**。

    最后一条不能漏：抖音图文号与文章号在同一张表里，但它们走 imageText 产线，
    文章驱动对它们必然报"平台不在文章发布支持范围"。不在这里挡掉，
    "全部自有账号"聚合（tenant-all）与智能分配会把它们排进文章发布队列。
    """
    from app.blueprints.distribution.service import article_platforms

    allowed = article_platforms()
    return [a for a in accounts
            if a.enabled and a.status == "active" and int(a.daily_limit) > 0
            and str(a.platform) in allowed]


def _expand_pool(accounts, channel_ids) -> list:
    """勾选项 id → 真实账号对象列表（虚拟聚合项展开为同平台全部真实账号）。

    聚合 id 规则（build_channels）：`tenant-all`=全租户、`{平台短码}-all`=该平台。
    """
    from app.blueprints.distribution.service import PLATFORM_CODE

    publishable = _publishable(accounts)
    by_id = {a.id: a for a in publishable}
    code_to_platform = {v: k for k, v in PLATFORM_CODE.items()}
    pool, seen = [], set()
    for cid in (channel_ids or []):
        cid = str(cid or "")
        if cid == "tenant-all":
            targets = list(publishable)
        elif cid.endswith("-all"):
            platform = code_to_platform.get(cid[:-4])
            targets = [a for a in publishable if platform and a.platform == platform]
        else:
            targets = [by_id[cid]] if cid in by_id else []
        for a in targets:
            if a.id not in seen:
                seen.add(a.id)
                pool.append(a)
    return pool


class TaskService:
    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    # ── 列表与运行态 ────────────────────────────────────────────────────────

    def list_tasks(self) -> dict:
        tid = current_tenant()
        repo = TenantScopedRepository(PublishTask, self.session)
        rows = repo.query().order_by(PublishTask.id.asc()).all()
        names = {a.id: a.name for a in TenantScopedRepository(Account, self.session).all()}
        tasks = [{"id": r.id, "title": r.article_dir, "platform": r.platform,
                  "status": _STATUS_TO_PAGE.get(r.status, "pending"),
                  "url": r.url or "", "error": r.error or "",
                  "account_name": names.get(r.account_id, r.account_id),
                  "created": hm_string(r.created_at), "finished": hm_string(r.finished_at)}
                 for r in rows]
        running = [r for r in rows if r.status in ("queued", "running")]
        current = next((r.article_dir for r in rows if r.status == "running"), "")
        return {"running": bool(running), "current": current, "tasks": tasks,
                "log_tail": executor.log_tail(tid)}

    # ── 排队发布 ────────────────────────────────────────────────────────────

    def queue_publish(self, body: dict) -> dict:
        dirs = [str(x) for x in (body.get("dirs") or []) if str(x).strip()]
        if not dirs:
            raise ValidationError("dirs 不能为空")
        accounts_sel = body.get("accounts")
        accounts = TenantScopedRepository(Account, self.session).all()
        if isinstance(accounts_sel, list) and accounts_sel:
            pool = _expand_pool(accounts, accounts_sel)
            if not pool:
                return {"ok": False, "error": "所选渠道没有可发布账号（额度/授权状态）"}
            plan = [(d, a) for d in dirs for a in pool]      # 每篇发所有勾选渠道
        else:
            pub = _publishable(accounts)
            if not pub:
                return {"ok": False, "error": "没有可用渠道，请先在「账号授权」添加"}
            plan = [(d, pub[i % len(pub)]) for i, d in enumerate(dirs)]  # 智能分配轮换

        tid = current_tenant()
        executor.clear_stop(tid)
        queued = 0
        for article_dir, account in plan:
            try:
                storage.read_article_file(article_dir, storage.FINAL)
            except Exception:  # noqa: BLE001 - 无终稿不进队列
                executor.task_log(tid, f"⚠ 「{article_dir}」没有终稿，跳过")
                continue
            executor.enqueue_publish(article_dir, account, session=self.session)
            queued += 1
        executor.task_log(tid, f"▶ 批量发布启动：{queued} 个任务 · "
                               + (f"{len(set(a.id for _, a in plan))} 个渠道"
                                  if isinstance(accounts_sel, list) and accounts_sel else "智能分配"))
        return {"ok": True, "queued": queued}

    def stop(self) -> dict:
        executor.request_stop(current_tenant())
        return {"ok": True}

    def resume(self) -> dict:
        """继续发布：清旗标，并把上次被取消的任务重投（当天幂等键不变 → 复用行）。"""
        tid = current_tenant()
        executor.clear_stop(tid)
        repo = TenantScopedRepository(PublishTask, self.session)
        rows = repo.query().filter(PublishTask.status == "canceled").all()
        accounts = {a.id: a for a in TenantScopedRepository(Account, self.session).all()}
        requeue: list = []
        for r in rows:
            acct = accounts.get(r.account_id)
            if acct is None:
                executor.task_log(tid, f"⚠ 「{r.article_dir}」账号 {r.account_id} 已不存在，跳过")
                continue
            r.status = "queued"
            r.finished_at = None
            requeue.append((r.id, r.idem_key))
        # 先 commit 再入队：worker 是独立连接，若在 commit 前读到旧状态（canceled）
        # 会按 `status != "queued"` 跳过，任务就永久卡在 queued 无人消费。
        self.session.commit()
        from app.platform import jobs
        for task_id, idem_key in requeue:
            jobs.enqueue("publish.run", {"task_id": task_id, "idem_key": idem_key},
                         tenant_id=tid, queue="publish")
        return {"ok": True, "requeued": len(requeue)}

    # ── 档案页单篇发布（archives.html）─────────────────────────────────────

    def pick_account(self) -> dict:
        """智能分配单发入口：今日用得最少的可发布账号优先（自然轮换）。"""
        accounts = TenantScopedRepository(Account, self.session).all()
        day = natural_day_string()
        pool = []
        for a in _publishable(accounts):
            used = int(a.used_today) if str(a.quota_date) == str(day) else 0
            if used < int(a.daily_limit):
                pool.append((used, a.id, a))
        if not pool:
            return {"ok": False, "error": "今日额度已用尽，请明天再试或添加账号"}
        pool.sort()
        return {"ok": True, "account_id": pool[0][1]}

    def publish_one(self, article_dir: str, account_id: str) -> dict:
        """发布一篇文章并**同步等结果**（fake 驱动毫秒级；页面读 {ok, url} 弹窗）。"""
        tid = current_tenant()
        if not article_dir:
            raise ValidationError("dir 必填")
        ck = storage.checkpoint(article_dir)
        if not ck[storage.FINAL]:
            return {"ok": False, "error": "该文章还没有终稿，无法发布"}

        accounts = TenantScopedRepository(Account, self.session).all()
        account = None
        if account_id:
            from app.blueprints.distribution.service import article_platforms

            account = next((a for a in accounts if a.id == str(account_id)), None)
            if account is None or not account.enabled or account.status != "active" \
                    or int(account.daily_limit) == 0 \
                    or str(account.platform) not in article_platforms():
                return {"ok": False,
                        "error": "渠道不可用：账号未授权/已冻结/登记型或非文章型平台不参与发布"}
        else:
            picked = self.pick_account()
            if not picked.get("ok"):
                return picked
            account = next(a for a in accounts if a.id == picked["account_id"])

        executor.clear_stop(tid)
        row = executor.enqueue_publish(article_dir, account, session=self.session)
        deadline = time.time() + _WAIT_S
        while time.time() < deadline:
            self.session.expire_all()
            fresh = (TenantScopedRepository(PublishTask, self.session).query()
                     .filter(PublishTask.id == row.id).first())
            if fresh is None:
                break
            if fresh.status in ("success", "failed", "canceled", "dead"):
                if fresh.status == "success":
                    return {"ok": True, "url": fresh.url or "", "task_id": fresh.id}
                return {"ok": False, "error": fresh.error or "发布失败", "task_id": fresh.id}
            time.sleep(0.1)
        return {"ok": False, "error": "发布进行中，稍后可在「发布日志」查看结果"}

    # ── 渠道体检 ────────────────────────────────────────────────────────────

    def preflight(self) -> dict:
        """按"能不能真发出去"体检：有 Cookie 或已绑蚁小二云账号、且账号活跃才算 ok。

        以前无条件 `ok=True`，页面"重新体检"永远全绿，用户以为做了凭证校验。
        真实 Playwright 逐渠道探活仍未接，但至少不再谎报健康。
        """
        accounts = TenantScopedRepository(Account, self.session).all()
        channels = []
        for a in accounts:
            if not a.enabled:
                continue
            ready = (bool(a.cookie_blob) or bool(a.yxer_id)) and a.status == "active"
            channels.append({"id": a.id, "name": a.name, "platform": a.platform,
                             "ok": ready, "status": a.status,
                             "reason": "" if ready
                             else "未配置 Cookie / 蚁小二账号，或账号非活跃"})
        return {"checking": False, "channels": channels,
                "bad": sum(1 for c in channels if not c["ok"])}
