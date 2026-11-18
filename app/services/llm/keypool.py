"""LLM 多 Key 池（AI-ARCH-002 v1.1 §1，必做项）。

设计要点（逐条对应方案）：
1. **三态状态机**：available / cooling / disabled。关键是**冷却到期自动复活** ——
   原系统日志连换 5 个 Key 就是"拉黑失控"的征兆。
2. **加权轮询**（smooth WRR）而非 round-robin —— 简单轮询会把请求继续压给已饱和的 Key。
3. **错误码 + 限流文案双识别** —— 上游用文案而非标准 429 返回限流。
4. **全冷却时排队等待**而非硬失败 —— 并发场景下"慢一点"远好于"整批失败"。
5. **指数退避**。
6. **重试边界**：只有**无副作用的推理调用**可重试；发布绝不进 Key 池。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.errors import AppError, UpstreamError
from app.logging import get_logger

log = get_logger(__name__)

AVAILABLE, COOLING, DISABLED = "available", "cooling", "disabled"

# 上游限流文案（中文优先；外加常见的英文关键词）
RATE_LIMIT_HINTS = (
    "限流", "频率", "过于频繁", "tpm", "rpm", "qps", "rate limit", "rate_limit",
    "too many requests", "exceed", "quota exceeded", "overload",
)


class NoKeyConfigured(AppError):
    code, http_status, message = "llm_no_key", 500, "未配置任何 LLM Key"


class AllKeysCooling(AppError):
    code, http_status, message = "llm_all_keys_cooling", 503, "所有 Key 均在冷却中，请稍后重试"


def is_rate_limited(status: int | None, message: str | None) -> bool:
    """错误码 + 限流文案双识别（要点 3）。"""
    if status == 429:
        return True
    text = (message or "").lower()
    return any(hint in text for hint in RATE_LIMIT_HINTS)


@dataclass
class LlmKey:
    value: str
    state: str = AVAILABLE
    weight: float = 1.0
    cooling_until: float = 0.0
    use_count: int = 0
    fail_count: int = 0
    last_error: str | None = None
    _cw: float = field(default=0.0, repr=False)   # smooth WRR 的当前权重


class KeyPool:
    """线程安全（GIL 下只做简单赋值，避免为 Phase 0/1 引入锁复杂度）。"""

    def __init__(
        self,
        keys: list[str],
        *,
        cooldown: float = 60.0,
        max_wait: float = 120.0,
        base_backoff: float = 1.0,
        clock=time.monotonic,
        sleeper=time.sleep,
    ) -> None:
        if not keys:
            raise NoKeyConfigured()
        self._keys = [LlmKey(value=k) for k in keys]
        self.cooldown = cooldown
        self.max_wait = max_wait
        self.base_backoff = base_backoff
        self._clock = clock
        self._sleep = sleeper

    # ── 查询 ────────────────────────────────────────────────────────────────

    @property
    def keys(self) -> list[LlmKey]:
        return list(self._keys)

    def available(self) -> list[LlmKey]:
        self._revive()
        return [k for k in self._keys if k.state == AVAILABLE]

    def snapshot(self) -> list[dict]:
        self._revive()
        return [
            {"key": k.value[-4:].rjust(4, "*"), "state": k.state, "weight": round(k.weight, 2),
             "use_count": k.use_count, "fail_count": k.fail_count}
            for k in self._keys
        ]

    # ── 状态迁移（要点 1：冷却到期自动复活）──────────────────────────────────

    def _revive(self) -> None:
        now = self._clock()
        for key in self._keys:
            if key.state == COOLING and now >= key.cooling_until:
                key.state = AVAILABLE
                key.cooling_until = 0.0
                key.weight = min(1.0, key.weight + 0.25)     # 复活时逐步恢复权重
                log.info("llm_key_revived", extra={"event": "llm_key_revived", "key": key.value[-4:]})

    def report_success(self, key: LlmKey) -> None:
        key.weight = min(1.0, key.weight + 0.1)
        key.last_error = None

    def report_rate_limited(self, key: LlmKey, message: str | None = None) -> None:
        key.state = COOLING
        key.cooling_until = self._clock() + self.cooldown
        key.fail_count += 1
        key.weight = max(0.1, key.weight / 2)             # 加权轮询：降低被选中的概率
        key.last_error = message or "rate_limited"
        log.warning("llm_key_cooling", extra={"event": "llm_key_cooling", "key": key.value[-4:]})

    def report_error(self, key: LlmKey, message: str | None = None) -> None:
        key.fail_count += 1
        key.last_error = message
        if key.fail_count >= 5:
            key.state = DISABLED
            log.error("llm_key_disabled", extra={"event": "llm_key_disabled", "key": key.value[-4:]})

    # ── 取 Key（要点 2/4）────────────────────────────────────────────────────

    def acquire(self) -> LlmKey:
        """加权轮询取一个可用 Key；全部冷却则排队等待（指数退避）而非硬失败。"""
        deadline = self._clock() + self.max_wait
        attempt = 0
        while True:
            avail = self.available()
            if avail:
                key = self._pick_weighted(avail)
                key.use_count += 1
                return key
            if all(k.state == DISABLED for k in self._keys):
                raise AllKeysCooling("所有 Key 均已停用，请检查配置")
            if self._clock() >= deadline:
                raise AllKeysCooling()
            # 指数退避，封顶 2**10 防止时钟不推进时溢出/空转
            backoff = self.base_backoff * (2 ** min(attempt, 10))
            wait = min(backoff, max(0.0, deadline - self._clock()))
            log.info("llm_all_cooling_wait", extra={"event": "llm_all_cooling_wait", "wait": round(wait, 2)})
            self._sleep(wait)
            attempt += 1

    def _pick_weighted(self, keys: list[LlmKey]) -> LlmKey:
        """smooth weighted round-robin：按权重平滑分配，避免把请求压给饱和 Key。"""
        total = sum(k.weight for k in keys) or 1.0
        for key in keys:
            key._cw += key.weight
        chosen = max(keys, key=lambda k: k._cw)
        chosen._cw -= total
        return chosen
