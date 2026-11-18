"""LLM 多 Key 池：三态 / 冷却复活 / 加权轮询 / 全冷却等待 / 限流双识别（AI-ARCH-002 §1）。"""
from __future__ import annotations

import pytest

from app.errors import UpstreamError
from app.services.llm import LLMClient, build_llm_client
from app.services.llm.keypool import (
    AVAILABLE,
    COOLING,
    AllKeysCooling,
    KeyPool,
    NoKeyConfigured,
    is_rate_limited,
)
from app.services.llm.providers import FakeProvider, LlmCallError


class FakeClock:
    """可推进的时钟，避免测试里真的 sleep。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _pool(keys=("k1", "k2", "k3"), clock=None, **kw) -> KeyPool:
    return KeyPool(list(keys), cooldown=kw.pop("cooldown", 10.0),
                   clock=clock or FakeClock(), sleeper=lambda _s: None, **kw)


# ── 限流双识别（错误码 + 文案）──────────────────────────────────────────────
def test_is_rate_limited_by_status_code():
    assert is_rate_limited(429, "")


def test_is_rate_limited_by_message():
    assert is_rate_limited(None, "inference exceeds tpm limit")
    assert is_rate_limited(None, "请求过于频繁，已限流")
    assert not is_rate_limited(500, "internal server error")
    assert not is_rate_limited(None, "")


# ── 三态与复活 ──────────────────────────────────────────────────────────────
def test_no_keys_raises():
    with pytest.raises(NoKeyConfigured):
        KeyPool([])


def test_rate_limited_key_enters_cooldown_then_revives():
    clock = FakeClock()
    pool = _pool(clock=clock)
    key = pool.acquire()
    pool.report_rate_limited(key, "限流")
    assert key.state == COOLING
    assert len(pool.available()) == 2          # 少了一个

    clock.advance(11)                          # 冷却到期
    assert len(pool.available()) == 3          # ★ 自动复活，不是永久拉黑
    assert key.state == AVAILABLE


def test_key_disabled_after_repeated_errors():
    pool = _pool(("k1",))
    key = pool.acquire()
    for _ in range(5):
        pool.report_error(key, "boom")
    assert key.state == "disabled"


# ── 加权轮询（不是简单轮询）──────────────────────────────────────────────────
def test_weighted_rotation_covers_all_keys():
    pool = _pool()
    picks = [pool.acquire().value for _ in range(9)]
    assert set(picks) == {"k1", "k2", "k3"}
    # 平滑加权轮询会均匀分布，而不是集中给某一个
    assert max(picks.count(k) for k in set(picks)) <= 5


def test_cooling_key_is_skipped_by_rotation():
    clock = FakeClock()
    pool = _pool(clock=clock)
    hot = pool.acquire()
    pool.report_rate_limited(hot, "限流")
    assert hot.value not in {pool.acquire().value for _ in range(6)}


# ── 全冷却：排队等待而非硬失败 ──────────────────────────────────────────────
def test_all_cooling_waits_then_raises_after_timeout():
    clock = FakeClock()
    waits: list[float] = []

    def sleeper(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)          # 真实 sleep 会推进时间，模拟之

    pool = KeyPool(["k1"], cooldown=100.0, max_wait=5.0, clock=clock, sleeper=sleeper)
    pool.report_rate_limited(pool.keys[0], "限流")
    with pytest.raises(AllKeysCooling):
        pool.acquire()
    assert waits, "全冷却时应先等待（指数退避），而不是立刻失败"
    assert clock.t >= 5.0, "应在 max_wait 之后才放弃"


def test_all_disabled_raises_without_waiting():
    pool = _pool(("k1",))
    key = pool.acquire()
    for _ in range(5):
        pool.report_error(key, "boom")
    with pytest.raises(AllKeysCooling):
        pool.acquire()


# ── 客户端重试：限流换 Key，其它错误退避重试 ────────────────────────────────
class FlakyProvider:
    name = "flaky"

    def __init__(self, fail_first: int, status: int | None, message: str) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.status, self.message = status, message

    def complete(self, messages, *, api_key, model=None, timeout=60.0, **kw):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise LlmCallError(self.status, self.message)
        return f"OK:{api_key}"


def test_client_switches_key_on_rate_limit():
    pool = _pool(("k1", "k2"))
    provider = FlakyProvider(fail_first=1, status=429, message="限流")
    client = LLMClient(pool, provider, sleeper=lambda _s: None)
    assert client.complete([{"role": "user", "content": "hi"}]) == "OK:k2"
    assert provider.calls == 2


def test_client_retries_then_gives_up():
    pool = _pool(("k1",))
    provider = FlakyProvider(fail_first=99, status=500, message="boom")
    client = LLMClient(pool, provider, max_retries=2, sleeper=lambda _s: None)
    with pytest.raises(UpstreamError):
        client.complete([{"role": "user", "content": "hi"}])


def test_fake_provider_is_deterministic():
    provider = FakeProvider()
    out1 = provider.complete([{"role": "user", "content": "甲"}], api_key="k")
    out2 = provider.complete([{"role": "user", "content": "甲"}], api_key="k")
    assert out1 == out2


class _Settings:
    llm_provider = "fake"
    llm_key_list = ["k1", "k2"]
    llm_cooldown_seconds = 30
    llm_max_retries = 2


def test_build_client_fake_provider():
    client = build_llm_client(_Settings())
    assert isinstance(client.provider, FakeProvider)
    assert len(client.pool.keys) == 2
