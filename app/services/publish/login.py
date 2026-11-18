"""专用通道账号的扫码登录（美篇 / 列表网 / 58 / 搜狐 / 百家号）。

为什么单独一个模块：发布链路是「每次发布新开**无痕**浏览器 + 注入 Cookie」
（无串味、无残留进程），而扫码登录需要「挂着一个**可见**浏览器等人扫」——
两套生命周期不同，硬塞进 `BrowserSession` 会把它搞脏。

设计要点：
- 每账号一个**持久化 profile**（`data/<租户>/browsers/<账号id>/`），兑现页面
  承诺的「每个号独立浏览器、互不影响」；
- 登录成功即抓 `context.cookies()` → JSON → Fernet 加密 → 写 `cookie_blob`；
  之后的发布仍旧走既有「无痕 + 注入 Cookie」通道，两边不耦合；
- Playwright 同步 API **非线程安全**，故只在扫码专属线程里被碰；
  轮询接口只读普通 dict，不接触浏览器对象；
- 会话表**有界**（上限 20 + 超时惰性清扫），不随租户/时间无限增长；
- Playwright 未装 → 如实报错并给安装命令，**绝不静默退化成假成功**。
"""
from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app.logging import get_logger
from app.services.publish.drivers import PublishError

log = get_logger(__name__)

LOGIN_TIMEOUT_S = 300          # 扫码等待上限（5 分钟）
_POLL_S = 2.0
_MAX_SESSIONS = 20             # 有界：超上限拒绝新登录，而不是无限增长

_LOCK = threading.Lock()
_SESSIONS: dict[str, dict] = {}


# ── 会话表（有界）────────────────────────────────────────────────────────────

def _sweep_locked() -> None:
    now = time.monotonic()
    for lid in [k for k, v in _SESSIONS.items() if v["deadline"] < now]:
        _SESSIONS.pop(lid, None)


def _get(login_id: str) -> dict | None:
    with _LOCK:
        return _SESSIONS.get(str(login_id or ""))


def _set(login_id: str, **fields) -> None:
    with _LOCK:
        st = _SESSIONS.get(login_id)
        if st is not None:
            st.update(fields)


def _own(login_id: str) -> dict | None:
    """取会话并校验归属租户。

    login_id 只是随机串、可被猜到；不校验租户等于让同实例的任意租户读到
    他人扫码状态、甚至代按"我扫完了"，多租户下属越权。查不到或不属于当前
    租户统一按"会话已结束"处理（不泄露该会话是否存在）。
    """
    from app.tenancy import current_tenant

    st = _get(login_id)
    if st is None or str(st.get("tenant_id") or "") != str(current_tenant()):
        return None
    return st


def reset_login_sessions() -> None:
    """测试隔离：清空扫码会话（进程内全局态）。"""
    with _LOCK:
        _SESSIONS.clear()


@contextmanager
def _tenant(tid: str):
    from app.tenancy import tenant_scope

    with tenant_scope(tid):
        yield


def _profile_dir_for(account_id: str, app) -> Path:
    """每账号独立的持久化 profile 目录（兑现"每个号独立浏览器"）。"""
    path = Path(app.config["DATA_ROOT"]) / "browsers" / str(account_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── 对外接口 ─────────────────────────────────────────────────────────────────

def start_login(account) -> str:
    """起一个可见浏览器打开平台首页，让人扫码/登录。返回 login_id（不阻塞）。"""
    from app.services.publish import browser as _browser
    from app.services.publish.platforms import get_adapter

    platform = str(getattr(account, "platform", "") or "")
    if get_adapter(platform) is None:
        raise PublishError(
            f"平台 {platform or '(未填)'} 暂无扫码登录适配器"
            "（支持：美篇 / 列表网 / 58 / 搜狐 / 百家号），可改用手填 Cookie",
            code="unknown")
    if not _browser.playwright_available():
        raise PublishError(
            "本机未安装 Playwright（扫码登录依赖）：请执行 "
            "`pip install playwright && python -m playwright install chromium`；"
            "或改用手填 Cookie / 蚁小二通道。", code="unknown")

    with _LOCK:
        _sweep_locked()
        if len(_SESSIONS) >= _MAX_SESSIONS:
            raise PublishError("同时进行的扫码登录过多，请先完成或取消一个", code="unknown")
        login_id = secrets.token_hex(8)
        _SESSIONS[login_id] = {
            "account_id": str(account.id), "tenant_id": str(account.tenant_id),
            "platform": platform, "deadline": time.monotonic() + LOGIN_TIMEOUT_S,
            "status": "waiting", "message": "正在打开浏览器…",
            "logged_in": False, "confirm": False,
        }

    from flask import current_app
    app = current_app._get_current_object()
    threading.Thread(target=_worker, args=(login_id, str(account.id), platform, app),
                     daemon=True, name=f"qrlogin-{login_id}").start()
    return login_id


def login_status(login_id: str) -> dict:
    st = _own(login_id)
    if st is None:
        return {"status": "gone", "message": "登录会话已结束", "logged_in": False}
    return {"status": st["status"], "message": st["message"],
            "logged_in": bool(st["logged_in"]), "platform": st["platform"]}


def confirm_login(login_id: str) -> dict:
    """手动确认：自动判定拿不准时，用户说"我扫完了"。"""
    if _own(login_id) is None:
        return {"ok": False, "error": "登录会话已结束"}
    with _LOCK:
        st = _SESSIONS.get(str(login_id or ""))
        if st is not None:
            st["confirm"] = True
    return {"ok": True}


def cancel_login(login_id: str) -> dict:
    if _own(login_id) is None:
        return {"ok": False, "error": "登录会话已结束或不属于当前租户"}
    _set(login_id, status="canceled", message="已取消")
    with _LOCK:
        _SESSIONS.pop(str(login_id or ""), None)
    return {"ok": True}


def verify_cookie(account) -> dict:
    """真实验证：用已存 Cookie 无痕访问平台首页，跑适配器的登录态判定。"""
    from app.services.publish import browser as _browser
    from app.services.publish.browser import wrap_playwright_error
    from app.services.publish.platforms import get_adapter

    adapter = get_adapter(str(getattr(account, "platform", "") or ""))
    if adapter is None:
        return {"ok": False, "error": f"平台 {account.platform} 无适配器，无法校验"}
    cfg = adapter.cfg
    domain = _browser.cookie_domain(cfg["home"])
    cookies = _browser.account_cookies(account, domain=domain)
    if not cookies:
        return {"ok": False, "error": "尚未保存 Cookie（请先扫码登录或手填 Cookie）"}
    try:
        # 同样以编辑器页为探针：能打开编辑器且不跳登录页，才叫登录态有效
        probe = cfg.get("editor") or cfg["home"]
        with _browser.BrowserSession(platform=adapter.platform, home=cfg["home"],
                                     cookies=cookies, headless=True) as page:
            page.goto(probe, wait_until="domcontentloaded")
            adapter.assert_logged_in(page)
    except Exception as exc:  # noqa: BLE001 - 统一归一化
        return {"ok": False,
                "error": wrap_playwright_error(exc, platform=adapter.platform).message}
    return {"ok": True, "msg": "登录态校验通过"}


# ── worker（扫码专属线程：Playwright 同步 API 只在本线程被碰）────────────────

def _worker(login_id: str, account_id: str, platform: str, app) -> None:
    from app.services.publish.platforms import get_adapter

    adapter = get_adapter(platform)
    # 探针用「编辑器页」而不是首页：未登录时站点会把人踢到登录页（美篇首页
    # 未登录也并不显示登录入口，拿首页判定必然误判成"已登录"）。
    probe = adapter.cfg.get("editor") or adapter.cfg["home"]
    pw = ctx = None
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        ctx = _launch_visible(pw, _profile_dir_for(account_id, app))
        page = ctx.new_page()
        page.goto(probe, wait_until="domcontentloaded")
        _set(login_id, status="waiting",
             message=f"已打开 {probe}，未登录会先跳到登录页，请在窗口里扫码/登录")

        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        while time.monotonic() < deadline:
            st = _get(login_id)
            if st is None or st["status"] in ("canceled", "done", "error"):
                return
            if st.get("confirm") or _logged_in(adapter, page, probe):
                from app.services.publish.login_store import capture_and_save

                capture_and_save(login_id, account_id, str(st.get("tenant_id") or ""),
                                 ctx, app, _set)
                return
            time.sleep(_POLL_S)
        _set(login_id, status="timeout", message="扫码超时（5 分钟未登录），请重试")
    except Exception as exc:  # noqa: BLE001 - 内核起不来等
        log.warning("qr_login_failed",
                    extra={"event": "qr_login_failed", "err": str(exc)[:160]})
        _set(login_id, status="error", message=f"浏览器启动失败：{str(exc)[:160]}")
    finally:
        _close_quietly(ctx, pw)


def _launch_visible(pw, user_data_dir: Path):
    """可见 + 持久化 profile；候选策略与发布驱动同一套（国内常下不动内核）。"""
    from app.services.publish import browser as _browser

    errors = []
    for name, kwargs in _browser._launch_candidates():
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(user_data_dir), headless=False,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                **kwargs)
            log.info("qr_login_browser",
                     extra={"event": "qr_login_browser", "strategy": name})
            return ctx
        except Exception as exc:  # noqa: BLE001 - 换下一个策略
            errors.append(f"{name}: {str(exc).splitlines()[0][:100]}")
    raise PublishError("浏览器内核不可用（已尝试：" +
                       "；".join(errors) + "）。装好 Edge/Chrome 即可自动使用。",
                       code="unknown")


def _logged_in(adapter, page, probe: str) -> bool:
    """登录态判定（探针 = 编辑器页）。

    停在登录页时先 reload 一次——扫码成功后站点会自己跳回编辑器，
    不 reload 的话主页面还停在登录页，会漏判。
    """
    cfg = adapter.cfg
    url = (page.url or "").lower()
    on_login_page = any(p.lower() in url for p in (cfg.get("login_url_patterns") or []))
    if on_login_page:
        try:
            page.goto(probe, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001 - 导航失败当未登录
            return False
    try:
        adapter.assert_logged_in(page)
        return True
    except Exception:  # noqa: BLE001 - 抛错即未登录
        return False


def _close_quietly(ctx, pw) -> None:
    for closer in (getattr(ctx, "close", None), getattr(pw, "stop", None)):
        try:
            if closer:
                closer()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结论
            pass


__all__ = ["LOGIN_TIMEOUT_S", "cancel_login", "confirm_login", "login_status",
           "reset_login_sessions", "start_login", "verify_cookie"]
