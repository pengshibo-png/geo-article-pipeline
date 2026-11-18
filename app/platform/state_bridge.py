"""进程内状态桥：租户 → 运行态状态的**有界**容器。

背景（2026-10-15 审计）：全仓 6 处状态桥原本都是裸 `dict`，每租户一条且
**永不回收** —— 租户多了就是内存泄漏；而这些进度本来就只在单进程内有效
（多副本部署时别的副本读不到），留着过期租户的状态毫无意义。

本容器提供：
- **有上限**：超过 cap 时一次性驱逐 1/4 最久未访问的租户（批量驱逐，避免
  每插入一个就触发一次驱逐）；
- 线程安全：取/建/驱逐都在同一把锁内。

注意：状态桥仍是**进程内**方案（重启丢进度、多副本失效）。彻底解决要上
Redis/RQ（Phase 4+ 待办），这里只解决"无限增长"。
"""
from __future__ import annotations

import threading

_DEFAULT_CAP = 200

# 这些键为真 = 该租户有"正在进行 / 待响应"的运行态（跑批中、已请求暂停或停止）。
# 驱逐它们等于弄丢进度条与 stop 旗标 —— "暂停发布"指令会静默失效、进度归零。
# 因此有界驱逐必须跳过这类租户：宁可少回收，也不可吞状态。
_ACTIVITY_KEYS = ("running", "stop", "pausing", "analyzing", "busy")


def _is_active(st) -> bool:
    return isinstance(st, dict) and any(bool(st.get(k)) for k in _ACTIVITY_KEYS)


class StateBridge:
    """`get(tid)` 返回该租户的状态 dict（不存在则按 factory 建一个）。"""

    def __init__(self, factory, cap: int = _DEFAULT_CAP, keep=None) -> None:
        self._factory = factory
        self._cap = max(4, int(cap))    # 下限只防误配成 0/1，别钳太死（测试用小 cap）
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._last: dict[str, int] = {}
        self._clock = 0
        self._keep = keep or _is_active

    def get(self, tid: str) -> dict:
        with self._lock:
            st = self._data.get(tid)
            if st is None:
                if len(self._data) >= self._cap:
                    self._evict_locked()
                st = self._factory()
                self._data[tid] = st
            self._clock += 1
            self._last[tid] = self._clock
            return st

    def _evict_locked(self) -> None:
        """驱逐最久未访问、且非活跃的租户（须持锁调用）。

        活跃租户（跑批中/已请求停止）整条跳过：驱逐会丢进度与 stop 旗标。
        若命中的全是被保护的，本轮就不回收 —— 内存多留一会儿，好过状态被吞。
        """
        drop = max(1, len(self._data) // 4)
        evicted = 0
        for tid, _ in sorted(self._last.items(), key=lambda kv: kv[1]):
            if evicted >= drop:
                break
            if self._keep(self._data.get(tid)):
                continue
            self._data.pop(tid, None)
            self._last.pop(tid, None)
            evicted += 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, tid: str) -> bool:
        with self._lock:
            return tid in self._data

    def clear(self) -> None:
        """清空（测试与 reset_states 复用）。"""
        with self._lock:
            self._data.clear()
            self._last.clear()


def bridge(factory, cap: int = _DEFAULT_CAP) -> StateBridge:
    """便捷构造：`_STATE = bridge(lambda: {"running": False})`。"""
    return StateBridge(factory, cap)


__all__ = ["StateBridge", "bridge"]
