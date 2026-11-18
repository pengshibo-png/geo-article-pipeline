"""抖音图文 · 「每条作品带自己的发布时间」：一次性定时，到点发一条。

用户 2026-10-19 定的口径：排期表里每条图文有自己的时刻（如 12:25 / 19:25），
到点把**这一包发到所有可用抖音号**（矩阵铺满），发过就不再发。与 autopub 的
「循环发布」是两条线：那条按"每 N 分钟发 1 篇"从货架上反复取包，这条每包一次。

数据落 `douyin/timed.json`（为何不与 pack.json 混写见 douyin_store 里的说明）：

    {"42": {"at": "2026-10-20 12:25", "state": "pending|fired|missed",
            "fired_at": "2026-10-20 12:25:07", "accounts": 11}}

定时发布是**无人值守**的，所以每一条"没发"都要如实记账（skipped 列表一路回传到
调度日志），`missed`（超过补发窗口）也要落盘并在页面上看得见 —— 否则用户只看到
"少发了两条"，再也没法归因。
"""
from __future__ import annotations

from datetime import datetime

from app.logging import get_logger
from app.platform import jobs
from app.services import douyin_accounts, douyin_store
from app.timeutil import shanghai

log = get_logger(__name__)

FANOUT_MODE = "each"     # 定时作品发到所有可用号（用户选择：11 个号各发一份）
CATCHUP_MIN = 180        # 迟到多久还算"该发没发"（应用没开着时）：3 小时
MAX_PER_TICK = 1         # 每个 tick 最多派一条：积压时不至于一次灌出去几十条

_FMTS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M")


def _aware(dt):
    """naive → aware：配置是本地墙钟串，而 tick 传的是 aware，不归一不能相减。"""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=shanghai())


def norm_at(raw) -> str:
    """把用户写的时间归一成 `YYYY-MM-DD HH:MM`；空/认不出返回 ""（调用方定夺）。"""
    s = " ".join(str(raw or "").replace("T", " ").split())
    return _fmt(s) if s else ""


def _fmt(s: str) -> str:
    for f in _FMTS:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return ""


def parse_at(text) -> datetime | None:
    when = norm_at(text)
    return _aware(datetime.strptime(when, "%Y-%m-%d %H:%M")) if when else None


def _dur(sec: float) -> str:
    m = int(max(0.0, sec) // 60)
    return f"{m // 60} 小时 {m % 60} 分" if m >= 60 else f"{m} 分钟"


# ── 写入清单 ────────────────────────────────────────────────────────────────

def set_at(pack_id, raw) -> str:
    """设置/清除某包的定时（空 = 取消）。返回归一后的时刻，认不出时为 ""。

    先校验包存在（不存在抛 NotFound，由调用方转成错误信息）：否则会写出一条
    永远发不出去、又清不掉的孤儿定时。
    """
    pack = douyin_store.read_pack(pack_id)
    when = norm_at(raw)
    with douyin_store.timed_lock():
        book = douyin_store.load_timed()
        if when:
            book[str(pack["id"])] = {"at": when, "state": "pending",
                                     "fired_at": "", "accounts": 0}
        else:
            book.pop(str(pack["id"]), None)
        douyin_store.save_timed(book)
    return when


def decorate(packs: list) -> list:
    """把定时字段挂到内容包上（页面按它显示 ⏰ 与状态，无需第二个请求）。"""
    book = douyin_store.load_timed()
    for p in packs:
        ent = book.get(str(p.get("id"))) or {}
        p["publish_at"] = str(ent.get("at") or "")
        p["publish_state"] = str(ent.get("state") or "")
        p["publish_fired_at"] = str(ent.get("fired_at") or "")
        p["publish_accounts"] = int(ent.get("accounts") or 0)
    return packs


def scheduled_count() -> int:
    """已排期还没发的条数（页面上"定时"筛选角标）。"""
    return sum(1 for e in douyin_store.load_timed().values()
               if isinstance(e, dict) and e.get("state") == "pending")


# ── 到点派单 ────────────────────────────────────────────────────────────────

def fire(tid: str, now: datetime, fired: list,
         skipped: list) -> tuple[list, list]:
    """把到点的定时作品交给发布队列（每包一条，发到所有可用号）。

    整段持 timed 锁：调度线程派单与 HTTP 保存定时会同时改这一份 JSON，
    不锁则刚记的 `fired` 会被一次保存覆盖 → 同一包发第二遍。
    读云账号（起一次蚁小二 CLI，约 0.4–1.0s）也在锁内 —— 只在真有到点包时才发生，
    换来的是"读到的账号"与"记下的条数"必定同一份。
    """
    now = _aware(now)
    with douyin_store.timed_lock():
        book = douyin_store.load_timed()
        ids = {str(p.get("id")) for p in douyin_store.list_packs()}
        ready, dirty = [], False
        for pid, ent in list(book.items()):
            if not isinstance(ent, dict) or ent.get("state") != "pending":
                continue
            if pid not in ids:                     # 内容包已删除 → 清掉孤儿定时
                del book[pid]
                dirty = True
                continue
            at = parse_at(ent.get("at"))
            if at is None:
                del book[pid]
                dirty = True
                continue
            late = (now - at).total_seconds()
            if late < 0:
                continue                           # 还没到点
            if late > CATCHUP_MIN * 60:
                ent["state"] = "missed"
                dirty = True
                skipped.append(
                    f"{tid}:douyin:timed:{pid}:已错过（迟到 {_dur(late)}，超过 "
                    f"{CATCHUP_MIN} 分钟补发窗口），请手动发布或重新设置时间")
                continue
            ready.append((at, pid))
        ready.sort(key=lambda x: x[0])             # 先发最早的那一条
        for at, pid in ready[:MAX_PER_TICK]:
            accs = [str(a["id"]) for a in douyin_accounts.list_accounts()
                    if a.get("status") == 1]
            if not accs:
                skipped.append(f"{tid}:douyin:timed:{pid}:无可用账号，"
                               "本次未发（下轮重试）")
                continue
            book[pid].update(state="fired", accounts=len(accs),
                             fired_at=now.strftime("%Y-%m-%d %H:%M:%S"))
            dirty = True
            # 记账（pending→fired，全工程只有这一处写）与派单在同一段锁内完成：
            # 万一 enqueue 抛异常，整段不落盘 → 下一轮重来，既不会丢也不会发两遍。
            jobs.enqueue("douyin.publish.batch",
                         {"pack_ids": [pid], "account_ids": accs,
                          "mode": FANOUT_MODE},
                         tenant_id=tid, queue="publish")
            late = (now - at).total_seconds()
            fired.append(f"{tid}:douyin:timed:{pid}:{len(accs)}"
                         + (f":迟到{int(late // 60)}分" if late > 60 else ""))
        if len(ready) > MAX_PER_TICK:
            skipped.append(f"{tid}:douyin:timed:另有 {len(ready) - MAX_PER_TICK} 条"
                          "已到点，下一轮继续（每分钟最多派一条）")
        if dirty:
            douyin_store.save_timed(book)
    return fired, skipped