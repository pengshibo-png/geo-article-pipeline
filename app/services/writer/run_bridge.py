"""受管批量生成的**状态桥**：进度/日志/成败计数（与任务体 managed.py 分离）。

原站页面（titles.html / gen.html）靠轮询 `/api/titles/generate-status` 与
`/api/status` 拿进度；ThreadPoolBackend 的约定是"状态在库、内存只持句柄"，
不覆盖这种细粒度进度。这里给一个**进程内状态桥**（模块级 dict + Lock）：

- 只在单进程部署（桌面 exe / 开发服务器）下有效；重启即清零 —— 页面读到
  running=False 是诚实降级（空闲），不丢数据（选题与文章在库在盘）。
- 多进程 / Redis+RQ 部署时由 worker 回报替代（ADR-004 阶段二，不在本轮）。

**锁约定**：threading.Lock 非重入 —— 一切"锁内读改"必须先在外面经访问器
取到 state 引用，绝不能在 `with _LOCK` 里再调访问器（B4 首版踩过自锁）。
"""
from __future__ import annotations

import threading
import time

from app.platform.state_bridge import bridge
from app.tenancy import current_tenant

_LOCK = threading.Lock()
_LOG_CAP = 400          # 状态桥里保留的日志行数（页面只展示尾部）
_TAIL_CAP = 100         # 单次快照返回的行数上限

# 两个桥都是**有界容器**（StateBridge）：超上限驱逐最久未访问的租户，
# 避免"每租户一条、永不回收"的内存泄漏。
# tid → {"running","stop","log_tail","titles","error"}（titles.generate.managed）
_TITLES = bridge(lambda: {"running": False, "stop": False, "log_tail": [],
                          "titles": [], "error": None, "target": 0})
# tid → {"running","pausing","stop","current","total","done","success","fail",
#        "log_tail","log_total","articles","error","started_at","last_activity"}
# started_at/last_activity 用于**僵尸批次租约**（见 gen_try_begin）。
_GEN = bridge(lambda: {"running": False, "pausing": False, "stop": False,
                       "current": "", "total": 0, "done": 0, "success": 0,
                       "fail": 0, "log_tail": [], "log_total": 0, "articles": [],
                       "error": None, "started_at": 0.0, "last_activity": 0.0})

# 一批写稿 30 分钟没有任何活动 = 僵尸（任务丢了/卡住），允许强制抢占复位。
_ZOMBIE_SEC = 1800.0


def _titles_state(tid: str) -> dict:
    return _TITLES.get(tid)     # 有界容器：超上限驱逐最久未访问的租户


def _gen_state(tid: str) -> dict:
    return _GEN.get(tid)


# ── 选题批量生成（titles 页）────────────────────────────────────────────────

def titles_running(tid: str) -> bool:
    return bool(_titles_state(tid)["running"])


def titles_begin(tid: str) -> None:
    st = _titles_state(tid)
    with _LOCK:
        st.update(running=True, stop=False, error=None, titles=[], target=0)
        st["log_tail"] = []


def titles_set_target(tid: str, target: int) -> None:
    """登记本批连跑的目标条数（供页面判定"达标"，替代日志文案匹配）。"""
    st = _titles_state(tid)
    with _LOCK:
        st["target"] = max(0, int(target or 0))


def titles_request_stop(tid: str) -> bool:
    st = _titles_state(tid)
    with _LOCK:
        if not st["running"]:
            return False
        st["stop"] = True
    return True


def titles_stop_requested(tid: str) -> bool:
    """任务循环轮询用：先取引用再进锁（访问器非重入约定）。"""
    st = _titles_state(tid)
    with _LOCK:
        return bool(st["stop"])


def titles_set_produced(tid: str, produced: list) -> None:
    st = _titles_state(tid)
    with _LOCK:
        st["titles"] = list(produced)


def titles_record_error(tid: str, message: str) -> None:
    st = _titles_state(tid)
    with _LOCK:
        st["error"] = message


def titles_finish(tid: str) -> None:
    st = _titles_state(tid)
    with _LOCK:
        st["running"] = False


def titles_log(tid: str, line: str) -> None:
    st = _titles_state(tid)
    with _LOCK:
        st["log_tail"].append(line)
        del st["log_tail"][:-_LOG_CAP]


def titles_snapshot(tid: str) -> dict:
    st = _titles_state(tid)
    with _LOCK:
        target = int(st.get("target") or 0)
        titles = list(st["titles"])
        return {"running": st["running"], "log_tail": list(st["log_tail"][-_TAIL_CAP:]),
                "error": st["error"], "titles": titles,
                "target": target,
                "target_met": bool(target) and len(titles) >= target}


# ── 文章批量生成（gen 页）───────────────────────────────────────────────────

def gen_running(tid: str) -> bool:
    return bool(_gen_state(tid)["running"])


def gen_try_begin(tid: str, total: int = 0) -> bool:
    """原子抢占一批写稿；已在跑返回 False。

    **为什么必须锁内 check-then-set**：`managed.begin_run` 里"是否在跑"的判断与
    后面的取题/补货（补货会同步调 LLM，可能阻塞几十秒）之间没有互斥。用户连点，
    或前端超时重试，会让两个请求拿到同一批选题、双份计费、并发生成互写同一目录，
    第二个 `gen_begin` 还会把 total/done 清零（进度失真）。

    **僵尸租约**：`running=True` 但 30 分钟没有任何活动（日志/进度都不动），说明
    任务已经丢了（入队失败 / 队列丢任务 / 进程卡住）。旧行为下此时页面永远
    显示"已有任务在运行"，暂停也不复位，只能重启进程 —— 超过租约就强制复位并抢占。
    """
    import time

    st = _gen_state(tid)
    now = time.time()
    with _LOCK:
        if st["running"]:
            last = float(st.get("last_activity") or st.get("started_at") or 0)
            if now - last < _ZOMBIE_SEC:
                return False
        st.update(running=True, pausing=False, stop=False, current="",
                  total=max(0, int(total or 0)), done=0, success=0, fail=0,
                  log_total=0, error=None, started_at=now, last_activity=now)
        st["log_tail"] = []
        st["articles"] = []
        return True


def gen_abort(tid: str, err: str) -> None:
    """启动阶段失败时复位批次。

    没有它的话：`gen_try_begin` 已把 running 置 True，之后 commit 或 enqueue 抛错
    就再没人复位 → 页面永久"运行中"、每次点生成都提示"已有任务在运行"。
    """
    import time

    st = _gen_state(tid)
    with _LOCK:
        st.update(running=False, pausing=False, stop=False, current="",
                  error=str(err or "")[:300], last_activity=time.time())


def gen_set_total(tid: str, total: int) -> None:
    """取题完成后回写真实条数（抢占时还不知道有几条）。"""
    st = _gen_state(tid)
    with _LOCK:
        st["total"] = max(0, int(total or 0))


def gen_begin(tid: str, total: int) -> None:
    """兼容旧调用：尽量抢占；抢不到说明已有任务在跑，**不再暴力覆盖计数**。"""
    gen_try_begin(tid, total)


def gen_request_pause(tid: str) -> bool:
    """置暂停标记：在跑的写完，还没开始的逐个跳过（选题退回）。"""
    st = _gen_state(tid)
    with _LOCK:
        if not st["running"]:
            return False
        st["pausing"] = True
        st["stop"] = True
    return True


def current_tid() -> str:
    return current_tenant()


def pause_run() -> dict:
    """暂停当前租户的写稿批次（HTTP 与调度共用出口）。"""
    if gen_request_pause(current_tenant()):
        return {"ok": True}
    return {"ok": False, "error": "当前没有运行中的写稿任务"}


def gen_should_skip(tid: str) -> bool:
    return bool(_gen_state(tid)["stop"])


def gen_log(tid: str, line: str) -> None:
    st = _gen_state(tid)
    with _LOCK:
        st["log_tail"].append(line)
        st["log_total"] += 1
        st["last_activity"] = time.time()      # 僵尸租约的心跳
        del st["log_tail"][:-_LOG_CAP]


def gen_start_article(tid: str, title: str) -> None:
    st = _gen_state(tid)
    with _LOCK:
        st["current"] = title
        st["last_activity"] = time.time()


def gen_finish_article(tid: str, title: str, *, ok: bool | None) -> None:
    """ok=None 表示因暂停跳过：计入 done（进度）但不进成败与产物列表。

    批次收口：done == total 时 running/pausing 归零 —— 页面轮询看到"空闲"，
    gen.html 的 setInterval 才会 clearInterval。
    """
    st = _gen_state(tid)
    with _LOCK:
        st["done"] += 1
        st["last_activity"] = time.time()
        if ok is not None:
            if ok:
                st["success"] += 1
            else:
                st["fail"] += 1
            st["articles"].append({"title": title, "ok": ok, "n": st["done"],
                                   "total": st["total"]})
        st["current"] = ""
        if st["done"] >= st["total"]:
            st["running"] = False
            st["pausing"] = False


def gen_snapshot(tid: str, *, lines: int = 200) -> dict:
    st = _gen_state(tid)
    tail = max(1, min(int(lines or 200), _LOG_CAP))
    with _LOCK:
        return {
            "running": st["running"], "pausing": st["pausing"], "current": st["current"],
            "progress": {"total": st["total"], "done": st["done"],
                         "success": st["success"], "fail": st["fail"]},
            "log_total": st["log_total"], "log_tail": list(st["log_tail"][-tail:]),
            "articles": list(st["articles"][-_TAIL_CAP:]),
        }


def reset_states() -> None:
    """清空两个状态桥。**仅供测试隔离**（进程内全局态会跨用例残留）。"""
    with _LOCK:
        _TITLES.clear()
        _GEN.clear()
