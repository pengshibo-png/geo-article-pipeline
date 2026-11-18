"""真实渠道驱动的浏览器层（Playwright）—— 平台无关的地基。

职责边界：
- 本模块只做「浏览器 + Cookie + 失败截图 + 错误归一化」，**不认识任何平台**；
  平台流程在 `services/publish/platforms/<平台>.py`（声明式选择器 + 通用流程）。
- Playwright 是**可选依赖**（延迟导入）：未安装时如实报错，**绝不静默退化成假链接**
  —— 把"没发出去"当成功是最坏的失败。
- 选择器可被 `data/publish_selectors.json` 覆盖：这些平台会改版，改配置不必改代码。

驱动选择（唯一判定入口 `resolve_mode`）：
  1. `GEO_PUBLISH_DRIVER=fake` → 一律离线假驱动（测试/演示，默认 auto）
  2. 账号绑定过蚁小二（yxer_id 非空）→ 走蚁小二通道（执行器优先）
  3. 平台没有浏览器适配器 → 假驱动
  4. 账号没配 Cookie → 假驱动（离线演示，任务日志如实标注"不会真的发出去"）
  5. 其余 → 真实驱动；Playwright 未安装则在发布时**报错**
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from app.logging import get_logger
from app.platform import storage
from app.services.publish import cookies as _cookies
from app.services.publish.drivers import PublishError, classify_error

log = get_logger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MODES = ("auto", "fake")
_SELECTOR_OVERRIDES: dict | None = None
_LAUNCH_CACHE: dict = {"strategy": None}     # 首次成功的内核策略，后续优先复用


# ── 可用性与模式判定 ────────────────────────────────────────────────────────

def playwright_available() -> bool:
    """playwright 包是否可用（浏览器内核是否下载过要等真正 launch 才知道）。"""
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - 未安装/装坏都算不可用
        return False


def _settings():
    try:
        from flask import current_app
        return current_app.config["GEO_SETTINGS"]
    except Exception:  # noqa: BLE001 - 无应用上下文（脚本/工具）时用默认
        return None


def setting(name: str, default=None):
    """读配置项（无应用上下文时给默认值，便于脚本/工具调用）。"""
    return getattr(_settings(), name, default) if _settings() is not None else default


def driver_mode() -> str:
    """auto（默认：账号配了 Cookie 就真发）/ fake（一律离线假驱动）。"""
    mode = str(getattr(_settings(), "publish_driver", "") or "auto").strip().lower()
    return mode if mode in MODES else "auto"


def resolve_mode(platform: str, account) -> tuple[str, str]:
    """返回 (模式, 说明)：`yxer` / `real` / `fake`。**驱动选择的唯一入口。**

    `real` 且 Playwright 不可用时**不在这里退化成 fake** —— 交给发布时报错，
    免得用户以为发出去了。
    """
    from app.services.publish.platforms import has_adapter

    if getattr(account, "yxer_id", None):
        return "yxer", "账号绑定蚁小二"
    if driver_mode() == "fake":
        return "fake", "离线模式（GEO_PUBLISH_DRIVER=fake）"
    if not has_adapter(platform):
        return "fake", f"平台 {platform} 暂无浏览器适配器"
    if not (account.cookie_blob if account is not None else None):
        return "fake", "账号未配置 Cookie → 离线模拟（不会真的发出去）"
    return "real", "真实浏览器驱动"


# ── Cookie ─────────────────────────────────────────────────────────────────

def cookie_domain(url: str) -> str:
    """由站点 URL 推 Cookie 域（实现在 cookies.py，此处 re-export 保持调用点不变）。"""
    return _cookies.cookie_domain(url)


def parse_cookies(raw: str, *, domain: str) -> list[dict]:
    """粘贴的 Cookie → Playwright cookie 列表（三种格式见 cookies.py）。"""
    return _cookies.parse_cookies(raw, domain=domain)


def cached_chromium() -> str | None:
    """ms-playwright 缓存里已下载过的 Chromium 可执行文件（版本号最大者）。"""
    import os

    root = Path(os.environ.get("LOCALAPPDATA") or "") / "ms-playwright"
    for pattern in ("chromium-*/chrome-win64/chrome.exe", "chromium-*/chrome-win/chrome.exe"):
        hits = sorted(root.glob(pattern))
        if hits:
            return str(hits[-1])
    return None


def _launch_candidates() -> list[tuple[str, dict]]:
    """启动策略（按顺序试）：自带内核 → 系统 Edge → 系统 Chrome → 缓存里的 Chromium。

    国内网络常常下不动 Playwright 自带内核（storage.googleapis.com 超时）；
    系统装了 Edge/Chrome 就照样能跑 —— 少一个"跑不起来"的死结。
    """
    out: list[tuple[str, dict]] = [("playwright-chromium", {})]
    out += [(f"system-{c}", {"channel": c}) for c in ("msedge", "chrome")]
    exe = cached_chromium()
    if exe:
        out.append((f"cached-chromium:{Path(exe).parents[1].name}", {"executable_path": exe}))
    return out


def account_cookies(account, *, domain: str) -> list[dict]:
    """解密账号 Cookie 并归一（密文由 services/crypto Fernet 保管）。"""
    return _cookies.account_cookies(account, domain=domain)


# ── 选择器覆盖（平台改版时改 data/publish_selectors.json，不必改代码）─────────

def selector_overrides(platform: str) -> dict:
    global _SELECTOR_OVERRIDES
    if _SELECTOR_OVERRIDES is None:
        path = Path(_data_root()) / "publish_selectors.json"
        try:
            _SELECTOR_OVERRIDES = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 没有覆盖文件是常态
            _SELECTOR_OVERRIDES = {}
    return dict(_SELECTOR_OVERRIDES.get(platform) or {})


def _data_root() -> str:
    try:
        from flask import current_app
        return current_app.config["DATA_ROOT"]
    except Exception:  # noqa: BLE001
        return "data"


def reset_selector_cache() -> None:
    """测试/热更新：丢掉选择器覆盖缓存。"""
    global _SELECTOR_OVERRIDES
    _SELECTOR_OVERRIDES = None


# ── 浏览器会话 ─────────────────────────────────────────────────────────────

class BrowserSession:
    """一次发布的浏览器会话：launch → context(注入 Cookie) → page → **用完即关**。

    每次发布新开浏览器（1~2 秒）换来三件事：无线程亲和性问题（发布跑在 worker
    线程池里）、无跨账号 Cookie 串味、无残留进程。
    """

    def __init__(self, *, platform: str, home: str, cookies: list[dict],
                 headless: bool = True, timeout_ms: int | None = None) -> None:
        self.platform = platform
        self.home = home
        self.cookies = cookies
        self.headless = headless
        self.timeout_ms = int(timeout_ms or setting("publish_timeout_ms", 30000))
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            raise PublishError(
                "本机未安装 Playwright（真实渠道驱动依赖）：请在源码态执行 "
                "`pip install playwright && python -m playwright install chromium`，"
                "或改用蚁小二通道发布。", code="unknown") from exc
        try:
            self._pw = sync_playwright().start()
            self._browser = self._launch()
        except Exception as exc:  # noqa: BLE001 - 多半是浏览器内核没下载
            self._stop()
            raise PublishError(f"浏览器内核启动失败：{str(exc)[:200]}", code="unknown") from exc
        self._context = self._browser.new_context(
            user_agent=DEFAULT_UA, locale="zh-CN", viewport={"width": 1440, "height": 900})
        self._context.set_default_timeout(self.timeout_ms)
        if self.cookies:
            self._context.add_cookies(self.cookies)
        self.page = self._context.new_page()
        return self.page

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and self.page is not None:
            self._screenshot(exc)
        self._stop()
        return False

    def _launch(self):
        """按候选策略启动，记住第一次成功的那个（失败一次要 1 秒上下，别反复试）。"""
        errors = []
        order = _launch_candidates()
        if _LAUNCH_CACHE["strategy"]:
            order.sort(key=lambda kv: kv[0] != _LAUNCH_CACHE["strategy"])
        for name, kwargs in order:
            try:
                browser = self._pw.chromium.launch(
                    headless=self.headless, args=["--disable-blink-features=AutomationControlled",
                                                  "--no-sandbox"], **kwargs)
                _LAUNCH_CACHE["strategy"] = name
                log.info("browser_launched", extra={"event": "browser_launched",
                                                    "strategy": name})
                return browser
            except Exception as exc:  # noqa: BLE001 - 换下一个策略
                errors.append(f"{name}: {str(exc).splitlines()[0][:120]}")
        raise PublishError(
            "浏览器内核不可用（已尝试：" + "；".join(errors) + "）。"
            "装好 Edge/Chrome 即可自动使用；或执行 `python -m playwright install chromium`"
            "（国内网络可加环境变量 PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright）",
            code="unknown")

    def _stop(self) -> None:
        for closer in (getattr(self._context, "close", None),
                       getattr(self._browser, "close", None),
                       getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:  # noqa: BLE001 - 关闭失败不影响发布结论
                pass
        self._context = self._browser = self._pw = None

    def _screenshot(self, exc: Exception) -> None:
        """失败现场留一张图（用户能自己看到卡在哪一步）。"""
        try:
            folder = storage.tenant_path("publish_debug")
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = folder / f"{stamp}-{self.platform}.png"
            self.page.screenshot(path=str(path), full_page=False)
            note = f"（失败现场截图：{path}）"
            if isinstance(exc, PublishError):
                exc.message = f"{exc.message}{note}"
            log.info("publish_screenshot", extra={"event": "publish_screenshot",
                                                  "path": str(path)})
        except Exception:  # noqa: BLE001 - 截图只是辅助，失败即放弃
            pass


def wrap_playwright_error(exc: Exception, *, platform: str) -> PublishError:
    """把 Playwright/驱动异常归一成 PublishError（错误码走 §1.5 正则表）。"""
    if isinstance(exc, PublishError):
        return exc
    text = str(exc) or exc.__class__.__name__
    code = classify_error(text)
    if code == "unknown" and ("Timeout" in exc.__class__.__name__ or "Timeout" in text):
        code = "timeout"
    return PublishError(f"[{platform}] {text[:300]}", code=code)


def sleep_brief(seconds: float = 0.4) -> None:
    """给页面一点反应时间（表单/编辑器初始化）。"""
    time.sleep(seconds)


__all__ = ["BrowserSession", "account_cookies", "cached_chromium", "cookie_domain",
           "driver_mode",
           "parse_cookies", "playwright_available", "reset_selector_cache",
           "resolve_mode", "selector_overrides", "setting", "sleep_brief",
           "wrap_playwright_error"]
