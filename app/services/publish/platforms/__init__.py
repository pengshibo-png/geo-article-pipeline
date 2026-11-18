"""平台适配器注册表 + 真实浏览器驱动。

新增平台 = 加一个 `platforms/<平台>.py` + 在 `_ADAPTER_CLASSES` 里登记一行，
执行器与接口层零改动（ADR-002 策略模式）。
"""
from __future__ import annotations

from app.logging import get_logger
from app.services.publish.drivers import PublishError
from app.services.publish.platforms.baijiahao import BaijiahaoAdapter
from app.services.publish.platforms.lieju import LiejuAdapter
from app.services.publish.platforms.meipian import MeipianAdapter
from app.services.publish.platforms.souhu import SouhuAdapter
from app.services.publish.platforms.wuba import WubaAdapter

log = get_logger(__name__)

_ADAPTER_CLASSES = (MeipianAdapter, SouhuAdapter, BaijiahaoAdapter, LiejuAdapter, WubaAdapter)
ADAPTERS: dict = {cls.platform: cls for cls in _ADAPTER_CLASSES}


def has_adapter(platform: str) -> bool:
    return platform in ADAPTERS


def get_adapter(platform: str, overrides: dict | None = None):
    """取平台适配器（选择器 = 代码默认值 ← data/publish_selectors.json 覆盖）。"""
    cls = ADAPTERS.get(platform)
    if cls is None:
        return None
    from app.services.publish import browser

    merged = {**browser.selector_overrides(platform), **(overrides or {})}
    return cls(merged)


class PlaywrightDriver:
    """真实浏览器驱动：解密 Cookie → 无头 Chromium → 平台适配器流程 → 文章 URL。

    每次发布新开一个浏览器上下文（用完即关）：发布跑在 worker 线程池里，
    这样既没有线程亲和性问题，也不会跨账号串 Cookie。
    """

    def __init__(self, platform: str, *, headless: bool | None = None,
                 timeout_ms: int | None = None) -> None:
        self.platform = platform
        self.adapter = get_adapter(platform)
        self.headless = headless
        self.timeout_ms = timeout_ms

    def publish(self, *, html: str, title: str, account) -> str:
        from app.services.publish import browser

        if self.adapter is None:
            raise PublishError(f"平台 {self.platform} 没有浏览器适配器", code="unknown")
        cfg = self.adapter.cfg
        domain = browser.cookie_domain(cfg["home"])
        cookies = browser.account_cookies(account, domain=domain)
        if not cookies:
            raise PublishError(
                f"账号 Cookie 解析为空（Cookie 域 {domain}）：请到「账号授权」"
                "重新提交完整 Cookie（支持 `a=1; b=2` 串 / 插件导出的 JSON 数组）",
                code="login_expired")
        headless = self.headless
        if headless is None:
            headless = bool(browser.setting("publish_headless", True))
        try:
            with browser.BrowserSession(
                    platform=self.platform, home=cfg["home"], cookies=cookies,
                    headless=headless, timeout_ms=self.timeout_ms) as page:
                return self.adapter.publish(page, html=html, title=title)
        except Exception as exc:  # noqa: BLE001 - 统一归一化成 PublishError
            raise browser.wrap_playwright_error(exc, platform=self.platform) from exc


def resolve_driver(platform: str, account):
    """按 `browser.resolve_mode` 的判定返回驱动实例（真实 or 离线假驱动）。"""
    from app.services.publish.browser import resolve_mode
    from app.services.publish.drivers import FakeDriver

    mode, reason = resolve_mode(platform, account)
    if mode == "real":
        return PlaywrightDriver(platform)
    return FakeDriver(note=reason)


__all__ = ["ADAPTERS", "PlaywrightDriver", "get_adapter", "has_adapter", "resolve_driver"]
