"""渠道驱动接口（ADR-002 策略模式：平台适配与任务执行解耦）。

两种驱动：
- **FakeDriver** —— 离线确定性假发布（无 Cookie / 演示模式 / 未适配平台），
  账号 id 以 `fail` 开头模拟验证码失败；
- **PlaywrightDriver**（`platforms/`）—— 真实浏览器驱动：搜狐/百家号/美篇/
  列表网/58同城。**选择逻辑的唯一入口是 `browser.resolve_mode`**：
  账号配了 Cookie 才走真实驱动，否则离线模拟（并在任务日志里如实标注）。

错误归一化沿用施工规格 §1.5 前端还原的熔断规则正则，
error_code 值域与 `models/publish.py:ERROR_CODES` 逐字对齐。
"""
from __future__ import annotations

import re
from typing import Protocol

from app.models.account import Account


class PublishError(Exception):
    """发布失败：message 人话原文 + code 归一化错误码。"""

    def __init__(self, message: str, *, code: str = "unknown") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# §1.5 规则 2/3/4/5：熔断/失效/额度/验证码 → 归一化码（顺序即优先级）
_PATTERNS = (
    (re.compile(r"熔断|限流|未生效过多|风控"), "rate_limited"),
    (re.compile(r"登录|未登录|cookie|Cookie|失效|过期"), "login_expired"),
    (re.compile(r"额度|上限|次数"), "quota"),
    (re.compile(r"验证码|captcha"), "captcha"),
    (re.compile(r"超时|timeout|连接", re.I), "timeout"),
)


def classify_error(text: str) -> str:
    for pattern, code in _PATTERNS:
        if pattern.search(text or ""):
            return code
    return "unknown"


class PublishDriver(Protocol):
    platform: str

    def publish(self, *, html: str, title: str, account: Account) -> str:
        """发布成功返回文章 URL；失败 raise PublishError。"""
        ...


class FakeDriver:
    """离线假发布：确定性成功并返回可点开的假链接。

    账号 id 以 `fail` 开头 → 抛验证码错误（测失败分支与错误归一化）。
    `note` 是给任务日志的说明（为什么走了离线驱动），执行器会原样打出来 ——
    离线模拟必须让用户看得见，不能悄悄假装成功。
    """

    platform = "fake"

    def __init__(self, note: str = "") -> None:
        self.note = note

    def publish(self, *, html: str, title: str, account: Account) -> str:
        if str(account.id or "").lower().startswith("fail"):
            raise PublishError("触发平台验证码：需人工处理后继续", code="captcha")
        slug = re.sub(r"[^\w一-鿿]+", "-", title or "")[:40].strip("-") or "article"
        return f"https://fake.example.com/{account.platform}/{slug}"


_FAKE = FakeDriver()


def get_driver(platform: str, account=None):
    """取驱动：带 account 时按「真实/离线」判定（唯一入口），否则一律假驱动。"""
    if account is not None:
        from app.services.publish.platforms import resolve_driver

        return resolve_driver(platform, account)
    return _FAKE


__all__ = ["PublishDriver", "PublishError", "FakeDriver", "classify_error", "get_driver"]
