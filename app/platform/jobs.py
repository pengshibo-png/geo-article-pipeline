"""任务队列统一接口（ADR-004）。

铁律：**业务代码只调 `enqueue()`，永不直接创建线程池。**
切换 Redis+RQ 时只替换 `ThreadPoolBackend` → `RQBackend`，`enqueue()` 签名不变，
业务代码 0 改动 —— 这是 ADR-004 最值钱的部分。

租户传递（ADR §2.5）：worker 没有请求上下文，所以租户**随 payload 走**，
`run_job` 进入时用 `tenant_scope()` 绑定。
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Protocol

from app.logging import get_logger
from app.tenancy import tenant_scope

log = get_logger(__name__)

QUEUES = ("default", "gen", "publish", "geo", "report")

TASKS: dict[str, Callable[[dict], Any]] = {}


def task(name: str):
    """把函数注册为可入队的任务。"""

    def _decorator(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
        if name in TASKS:
            raise ValueError(f"任务名重复注册: {name}")
        TASKS[name] = fn
        return fn

    return _decorator


class JobBackend(Protocol):
    def enqueue(self, task_name: str, payload: dict, *, tenant_id: str,
                queue: str = "default", priority: int = 0) -> str: ...

    def cancel(self, job_id: str) -> None: ...

    def shutdown(self, wait: bool = True) -> None: ...


class ThreadPoolBackend:
    """进程内实现（ADR-004 阶段一）。**状态在数据库，内存只持有句柄。**"""

    def __init__(self, app: Any, pools: dict[str, ThreadPoolExecutor]) -> None:
        self.app = app
        self.pools = pools
        self._futures: dict[str, Any] = {}

    def enqueue(self, task_name: str, payload: dict, *, tenant_id: str,
                queue: str = "default", priority: int = 0) -> str:
        if task_name not in TASKS:
            raise KeyError(f"未注册的任务: {task_name}")
        if not tenant_id:
            raise ValueError("enqueue 必须携带 tenant_id")
        job_id = uuid.uuid4().hex
        pool = self.pools.get(queue) or self.pools["default"]
        # ★ 租户随任务走（ADR §2.5）
        wrapped = {**payload, "_tenant_id": tenant_id, "_job_id": job_id}
        future = pool.submit(self._run, task_name, wrapped)
        self._futures[job_id] = future
        # 完成即移除句柄：这张表以前只增不减（cancel 只在排队期有意义），
        # 桌面常驻进程下是实打实的内存泄漏。
        future.add_done_callback(
            lambda f, jid=job_id, name=task_name: self._reap(f, jid, name))
        log.info("job_enqueued", extra={"event": "job_enqueued", "task": task_name, "queue": queue})
        return job_id

    def _reap(self, future: Any, job_id: str, task_name: str) -> None:
        """任务收尾（在 worker 线程执行）：清句柄 + **记录未被捕获的异常**。

        以前这里只 pop 句柄、从不读 `exception()`：线程池会把异常静静吃掉，
        于是"批量写稿 19 篇失败"在磁盘日志里一行痕迹都没有，用户只能看到
        少生成几篇却永远查不到原因。
        """
        self._futures.pop(job_id, None)
        if future.cancelled():
            return
        try:
            exc = future.exception()
        except Exception:  # noqa: BLE001 - 取异常本身不该再抛
            return
        if exc is not None:
            log.error("job_failed", extra={
                "event": "job_failed", "task": task_name, "job_id": job_id,
                "err": f"{type(exc).__name__}: {str(exc)[:300]}"})

    def _run(self, task_name: str, payload: dict) -> Any:
        # 线程内没有 Flask 上下文 → 显式开一个
        with self.app.app_context():
            return run_job(task_name, payload)

    def cancel(self, job_id: str) -> None:
        future = self._futures.get(job_id)
        if future is not None:
            future.cancel()

    def shutdown(self, wait: bool = True) -> None:
        for pool in self.pools.values():
            pool.shutdown(wait=wait)


def run_job(task_name: str, payload: dict) -> Any:
    """worker 入口。进入即绑定租户 —— 禁止在任务里读 session/g。

    **会话边界**：worker 会连续处理不同租户的任务，而会话是共享的。
    若不在每个任务后提交/清理，上一个租户的脏对象会在下一个租户的 flush 时
    触发 TenantViolation（守卫是对的，但会误伤下一个任务）。所以这里
    显式 commit / rollback + remove —— 一个任务一个事务边界。
    """
    tenant_id = payload.get("_tenant_id")
    if not tenant_id:
        raise ValueError("任务 payload 缺少 _tenant_id")
    fn = TASKS.get(task_name)
    if fn is None:
        raise KeyError(f"未注册的任务: {task_name}")

    from app.extensions import db

    with tenant_scope(tenant_id):
        try:
            result = fn(payload)
            db.session.commit()
            return result
        except Exception:
            db.session.rollback()
            raise
        finally:
            db.session.remove()


_backend: JobBackend | None = None


def init_jobs(app: Any) -> JobBackend:
    global _backend
    size = int(app.config.get("GEN_POOL_SIZE", 4))
    pools = {
        q: ThreadPoolExecutor(max_workers=size, thread_name_prefix=f"geo-{q}") for q in QUEUES
    }
    _backend = ThreadPoolBackend(app, pools)
    return _backend


def get_backend() -> JobBackend:
    if _backend is None:
        raise RuntimeError("jobs 未初始化：请先调用 init_jobs(app)")
    return _backend


def enqueue(task_name: str, payload: dict, *, tenant_id: str,
            queue: str = "default", priority: int = 0) -> str:
    return get_backend().enqueue(task_name, payload, tenant_id=tenant_id,
                                 queue=queue, priority=priority)


def shutdown_jobs(wait: bool = True) -> None:
    global _backend
    if _backend is not None:
        _backend.shutdown(wait=wait)
        _backend = None
