"""LLM 供应商适配（策略模式 + 注册表，ADR-007）。

Phase 0/1 只保证两件事：
- `FakeProvider` 让整条流水线**离线可跑、可测**（不依赖外网）；
- `OpenAICompatProvider` 用标准库 urllib，零额外依赖，兼容 DeepSeek/火山方舟/百炼等
  OpenAI 兼容端点。真实 Key 与 base_url 由配置注入，不硬编码。
"""
from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

from app.platform import net

# 抖音图文文案的假实现：每次调用换标题/正文，避免两次生成被判重后回落兜底
_COPY_SEQ = itertools.count(1)


class LlmCallError(Exception):
    """携带状态码与文案，便于上层做"错误码 + 限流文案双识别"。"""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ChatProvider(Protocol):
    name: str

    def complete(self, messages: list[dict], *, api_key: str,
                 model: str | None = None, timeout: float = 60.0, **kw: Any) -> str: ...


class FakeProvider:
    """确定性假供应商：同样输入 → 同样输出。用于测试与本地跑通。"""

    name = "fake"

    def __init__(self, responder: Callable[[list[dict]], str] | None = None) -> None:
        self._responder = responder or self._default

    @staticmethod
    def _default(messages: list[dict]) -> str:
        user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        if "审稿主编" in user:
            # B 段：返回能通过冻结契约（validate_review.py）的审稿 JSON。
            # location 必须是待审文章的真实片段 —— 直接从 Prompt 里的原文抠一段。
            anchor = user.find("【待审文章】")
            frag = ""
            if anchor >= 0:
                line = user[anchor + len("【待审文章】"):].lstrip().split("\n", 1)[0]
                frag = line.strip()[:20]
            review = {
                "overall_score": 82,
                "verdict": "需要修改",
                "summary": "[FAKE] 整体成立，品牌解决方案展开不足",
                "issues": [{
                    "severity": "建议",
                    "dimension": "维度五：品牌融入质量",
                    "location": frag or "正文",
                    "problem": "[FAKE] 品牌解决方案一笔带过",
                    "suggestion": "[FAKE] 补充怎么解决、解决到什么程度",
                }],
                "highlights": ["[FAKE] 首段结论明确"],
            }
            return json.dumps(review, ensure_ascii=False)
        if "只输出一个 JSON 对象" in user:
            # 抖音图文文案：返回能通过结构校验的 JSON。标题/正文随序号变化 ——
            # 否则第二次生成会与第一次完全重复 → 判重 → 兜底 → 被"兜底不进配图"拦下。
            n = next(_COPY_SEQ)
            desc = "".join(
                f"第{n}篇第{i}条{n}{i}：断口{n}能否对位{i}，方案{n}再定{i}。"
                for i in range(30))
            return json.dumps({"title": f"断镯修复知识{n}",
                               "description": desc,
                               "tags": ["断镯修复", "知识分享"]},
                              ensure_ascii=False)
        return f"[FAKE] 针对以下要求生成的正文占位：{user[:120]}"

    def complete(self, messages: list[dict], *, api_key: str,
                 model: str | None = None, timeout: float = 60.0, **kw: Any) -> str:
        return self._responder(messages)


class OpenAICompatProvider:
    """OpenAI 兼容端点（DeepSeek / 火山方舟 / 阿里百炼 等）。"""

    name = "openai-compat"

    def __init__(self, base_url: str, model: str = "deepseek-chat") -> None:
        if not base_url:
            raise ValueError("base_url 不能为空")
        self.base_url = base_url.rstrip("/")
        self.model = model

    def complete(self, messages: list[dict], *, api_key: str,
                 model: str | None = None, timeout: float = 60.0, **kw: Any) -> str:
        body = {
            "model": model or self.model,
            "messages": messages,
            "temperature": kw.get("temperature", 0.7),
        }
        # 输出预算：模型默认上限常被 reasoning（思维链）吃掉，长文会写到一半就停。
        # 需要长输出的调用方显式给 max_tokens —— 它只是**上限**，不会增加成本。
        if kw.get("max_tokens"):
            body["max_tokens"] = int(kw["max_tokens"])
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            # 走 net.urlopen：忽略注册表系统代理（VPN 代理会断 TLS，见 platform/net.py）
            with net.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:                       # 4xx/5xx
            raw = exc.read().decode("utf-8", errors="replace")
            raise LlmCallError(exc.code, raw) from exc
        except (urllib.error.URLError, TimeoutError) as exc:        # 网络/超时
            raise LlmCallError(None, f"network error: {exc}") from exc

        choice = (data.get("choices") or [{}])[0]
        # finish_reason=length = 被输出上限截断，正文是**半截**。以前这里完全不看，
        # 于是"写到一半的文案"静默落盘（实测 8 个抖音包里 3 个是半截，用户能看到断句）。
        # 现在抛错 → LLMClient 重试；仍截断则调用方走兜底并如实标注。
        if str(choice.get("finish_reason") or "") == "length":
            raise LlmCallError(None, "输出被截断（finish_reason=length）：请调大 max_tokens")
        try:
            return choice["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LlmCallError(None, f"响应结构异常: {str(data)[:200]}") from exc
