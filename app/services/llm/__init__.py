"""LLM 客户端：Key 池 + 供应商 + 重试退避（AI-ARCH-002 v1.1）。

**重试边界（要点 6）**：只有无副作用的推理调用可重试。发布类操作**绝不**走这里
—— 重复发布 = 事故，必须由 `publish_task` 的幂等键兜底。
"""
from __future__ import annotations

import time

from app.errors import UpstreamError
from app.logging import get_logger
from app.services.llm.keypool import KeyPool, is_rate_limited
from app.services.llm.providers import ChatProvider, FakeProvider, LlmCallError, OpenAICompatProvider

log = get_logger(__name__)

# 输出上限的天花板：截断时按倍递增预算，但不能涨过这个值（否则端点直接 400）。
# 实测 deepseek-flash（reasoning 模型）接受 32768；调大只是**上限**，不增加成本。
_MAX_TOKEN_CEILING = 32768


class LLMClient:
    def __init__(
        self,
        pool: KeyPool,
        provider: ChatProvider,
        *,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        sleeper=time.sleep,
    ) -> None:
        self.pool = pool
        self.provider = provider
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self._sleep = sleeper

    def complete(self, messages: list[dict], *, model: str | None = None, **kw) -> str:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            key = self.pool.acquire()
            try:
                text = self.provider.complete(messages, api_key=key.value, model=model, **kw)
                self.pool.report_success(key)
                return text
            except LlmCallError as exc:
                last = exc
                if is_rate_limited(exc.status, exc.message):
                    # 限流 → 冷却该 Key，立刻换下一个（不消耗退避）
                    self.pool.report_rate_limited(key, exc.message)
                    continue
                self.pool.report_error(key, exc.message)
                # ★ 截断是**预算不足**，不是瞬时故障：同样的 max_tokens 重试必然再截断。
                # 旧行为是原样重试 3 次（3 次白烧 token）后抛 UpstreamError，调用方
                # 只能降级交付 —— 实测一次审稿因此把 56 分的初稿当定稿发了出去。
                # 正确做法：把预算翻倍再试（reasoning 模型的思维链会先吃掉一大截）。
                mt = int(kw.get("max_tokens") or 0)
                if "截断" in str(exc.message) and mt and mt < _MAX_TOKEN_CEILING:
                    nxt = min(mt * 2, _MAX_TOKEN_CEILING)
                    kw["max_tokens"] = nxt
                    log.warning("llm_truncated_budget_raised", extra={
                        "event": "llm_truncated_budget_raised",
                        "from": mt, "to": nxt, "attempt": attempt + 1})
                    continue
                if attempt >= self.max_retries:
                    break
                self._sleep(self.base_backoff * (2 ** attempt))
        raise UpstreamError(f"LLM 调用失败（已重试 {self.max_retries} 次）：{last}")


_PRESET_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openai": "https://api.openai.com/v1",
}


def build_llm_client(config) -> LLMClient:
    """按配置装配。provider=fake 时无需任何 Key 也能跑通整条流水线。

    **任意模型**：provider=`openai`（或 `openai-compat`/`custom`）+ `base_url`
    指向任何 OpenAI 兼容端点（Moonshot/Zhipu/SiliconFlow/OpenRouter/本地 Ollama…），
    `model` 填该端点的模型名。

    **在线覆盖**：`app/services/model_config.effective_llm` 合并"系统设置页保存的
    在线配置"与环境变量，在线优先 —— 用户在 /settings 改模型无需重启。config 参数
    仍是 BaseSettings（提供环境变量兜底与 cooldown/retries）。
    """
    from app.services.model_config import effective_llm

    eff = effective_llm(config)
    provider_name = (eff["provider"] or "fake").lower()
    if provider_name == "fake":
        keys = eff["keys"] or ["fake-key-1"]
        provider: ChatProvider = FakeProvider()
    elif provider_name in _PRESET_URLS or provider_name in ("openai-compat", "custom"):
        keys = eff["keys"]
        if not keys:
            raise ValueError("该 provider 需要 API Key：请在系统设置里配置，或切回 fake")
        base_url = (eff["base_url"] or "").strip() or _PRESET_URLS.get(provider_name, "")
        if not base_url:
            raise ValueError(f"provider={provider_name} 需要 base_url（OpenAI 兼容端点 URL）")
        provider = OpenAICompatProvider(base_url=base_url,
                                        model=(eff["model"] or "").strip() or "deepseek-chat")
    else:
        raise ValueError(f"未知的 LLM provider: {provider_name}")

    pool = KeyPool(keys, cooldown=float(eff["cooldown"]))
    return LLMClient(pool, provider, max_retries=int(eff["max_retries"]))


def build_engine_client(cfg: dict) -> LLMClient:
    """单个 GEO 引擎的独立端点客户端（base_url/model/key 三件套）。

    与 build_llm_client 的区别：不走全局 provider 与 Key 池，每个引擎问自己的
    模型 —— 四引擎共用一个客户端的话，四份答案其实出自同一模型，监测数据
    会失真（口径规则 AI-ARCH-002 要求逐引擎采样）。
    """
    base_url = str(cfg.get("base_url") or "").strip()
    key = str(cfg.get("key") or "").strip()
    if not base_url or not key:
        raise ValueError("引擎配置需要 base_url 与 key")
    provider = OpenAICompatProvider(
        base_url=base_url,
        model=str(cfg.get("model") or "").strip() or "deepseek-chat")
    return LLMClient(KeyPool([key]), provider, max_retries=2)
