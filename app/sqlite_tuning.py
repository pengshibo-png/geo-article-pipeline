"""SQLite 并发加固（桌面单机 exe / 本地开发路径）。

背景：桌面版用 waitress `threads=8` + 后台调度线程并发访问同一个 `app.db`，
而 SQLite 默认**未开 WAL**、写锁等待极短，上传素材这类"读表 + INSERT + commit"
的写请求会偶发 `sqlite3.OperationalError: database is locked`（表现为 500 / internal_error）。

对策（每条连接的 connect 事件上执行，幂等）：
- `PRAGMA journal_mode=WAL`  —— 读写不再互斥，写不阻塞读，并发显著改善；
- `PRAGMA busy_timeout=N`    —— 抢不到写锁时等待而非立刻报错（默认 5s 太短）；
- `PRAGMA synchronous=NORMAL`—— WAL 下的安全/性能折中。

只对 SQLite 方言生效；PostgreSQL（SaaS 路径）直接跳过，由连接池与 RLS 负责。

## 2026-10-16 实测：真正的坑不在 PRAGMA，而在"写事务跨越 LLM 调用"

批量写稿 4 个 worker 并发时报 `database is locked`，现场是这一条：

    INSERT INTO article (...) RETURNING id  →  (sqlite3.OperationalError) database is locked

排查结论（按重要性）：

1. **主因：写事务被跨着网络等待攥住几十秒。**
   `run_job` 整个任务是一个事务，而 A/B/C 每段都有 30~60 秒的 LLM 调用；
   SQLAlchemy 又是**延迟 flush**（`repo.add()` 不会立刻发 INSERT，等到下一次
   查询或 commit 才发），于是"读快照 → 60 秒后升级为写"必然撞车。
   修法：**即写即交** —— 每个阶段写完立刻 `commit()`（见 writer/pipeline.py、
   review.py、rewrite.py、managed.py）。这条才是根治。

2. **busy_timeout 对"延迟事务升级"无效。** 快照冲突（读事务升级为写事务时
   发现别人已写过）SQLite 会**立刻**返回 BUSY 且**不调用 busy handler**，
   所以 30s 的超时形同虚设 —— 但只要事务足够短（上面第 1 条），窗口就是毫秒级。

3. **试过并放弃 `BEGIN IMMEDIATE`**（让事务开头就抢写锁，busy handler 就一定会
   生效）。它确实能治升级冲突，但**读事务也要抢写锁** —— 本应用还有 GEO 监测、
   报告生成等同样跨着 LLM 的长事务路径，一旦加上就等于"任何长事务都会冻结整个
   UI 的读取"。收益不抵风险，故不采用。要重启这条路线，前提是先把全仓的长事务
   都改成即写即交。
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.logging import get_logger

log = get_logger(__name__)

_BUSY_TIMEOUT_MS = 30_000          # 写锁最长等待 30s（单机写入都是毫秒级，足够）


def install_sqlite_pragmas(engine: Engine) -> None:
    """给 SQLite 引擎挂上 WAL + busy_timeout（非 SQLite 方言为空操作）。"""
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):        # noqa: ANN001 - SQLAlchemy 事件签名
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()

    log.info("sqlite 并发加固已启用",
             extra={"event": "sqlite_pragmas", "busy_timeout_ms": _BUSY_TIMEOUT_MS})


__all__ = ["install_sqlite_pragmas"]
