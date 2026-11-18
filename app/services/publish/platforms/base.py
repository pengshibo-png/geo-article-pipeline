"""平台适配器基类：**声明式选择器 + 一套通用流程**。

设计取舍（原站经验：平台会改版，且改版只改选择器）：
- 每个平台只声明一份 CONFIG（URL / 候选选择器 / 成功判定），流程代码**全平台共用**；
- 候选选择器是**列表**，逐个试，第一个"存在且可见"的胜出 —— 一个候选失效不致命；
- CONFIG 可被 `data/publish_selectors.json` 覆盖（`{"meipian": {"title": [...]}}`），
  改版时改配置即可，不必改代码、不必重新打包。

流程：goto(editor) → 登录态自检 → 填标题 → 填正文 → 传图 → 点发布 → 等成功判定。
任一步骤失败都抛 PublishError（错误码走 §1.5 归一化表，执行器据此熔断/标失效）。
"""
from __future__ import annotations

import re
import time

from app.logging import get_logger
from app.services.publish.drivers import PublishError

log = get_logger(__name__)

_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_BR = re.compile(r"</(p|div|h[1-6]|li|tr|table|section)>", re.I)
_TAGS = re.compile(r"<[^>]+>")

# 风控验证码（腾讯/极验/网易易盾…）：命中即如实报 captcha，**不要**继续往下填表单
_CAPTCHA_SELECTORS = (
    "iframe[src*='captcha']", "#tcaptcha_iframe_dy", ".nc_wrapper", ".nc-container",
    "div[class*='geetest']", "iframe[src*='yidun']", "div[class*='captcha']",
)


def plain_text(html: str) -> str:
    """HTML → 纯文本（编辑器不支持富文本时的兜底；保留段落换行）。

    顺序要紧：先摘掉 script/style 的**内容**，再把块级结束标签换成换行，
    最后才去掉剩下的标签 —— 反过来做会把换行一起吃掉。
    """
    text = _TAGS.sub("", _BR.sub("\n", _TAG.sub("", html or "")))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def first_visible(page, selectors, *, timeout: int = 2500, root=None):
    """候选选择器逐个试，返回第一个"存在且可见"的 Locator；都没有 → None。"""
    scope = root if root is not None else page
    for sel in selectors or []:
        try:
            loc = scope.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:  # noqa: BLE001 - 候选不匹配是常态，继续试下一个
            continue
    return None


class Adapter:
    """平台适配器基类。子类只需给出 platform / name / CONFIG。"""

    platform = ""
    name = ""
    CONFIG: dict = {}

    def __init__(self, overrides: dict | None = None) -> None:
        self.cfg = {**self.CONFIG, **(overrides or {})}

    # ── 对外：发布一篇 ─────────────────────────────────────────────────────
    def publish(self, page, *, html: str, title: str) -> str:
        cfg = self.cfg
        page.goto(cfg["editor"], wait_until="domcontentloaded")
        self.wait_ready(page)
        self.assert_logged_in(page)
        self.assert_no_captcha(page)
        self.fill_title(page, title)
        self.fill_body(page, html)
        self.upload_images(page, cfg.get("image_files") or [])
        self.run_steps(page, cfg.get("extra_steps") or [])
        self.click_submit(page)
        return self.wait_success(page)

    def wait_ready(self, page, timeout_ms: int = 8000) -> None:
        """等页面"到位"（编辑器 or 登录墙 or 验证码谁先出现都算）。

        少了这一步会把"登录弹窗还没渲染出来"误判成"找不到标题框 → 点击超时"，
        错误码就归错类了（应报 login_expired，而不是 timeout）。
        """
        body_cfg = self.cfg.get("body") or {}
        candidates = (list(self.cfg.get("title") or [])
                      + list(body_cfg.get("selectors") or [])
                      + list(self.cfg.get("login_selectors") or [])
                      + ["iframe[src*='captcha']", "#tcaptcha_iframe_dy"])
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            for sel in candidates:
                try:
                    if page.locator(sel).first.count() > 0:
                        return
                except Exception:  # noqa: BLE001
                    continue
            page.wait_for_timeout(300)

    # ── 登录态 ────────────────────────────────────────────────────────────
    def assert_logged_in(self, page) -> None:
        """Cookie 失效要报 login_expired（执行器据此把账号标记 invalid）。"""
        url = (page.url or "")
        for pat in self.cfg.get("login_url_patterns") or []:
            if pat in url:
                raise PublishError(f"[{self.name}] 登录态已失效（被跳到登录页 {url}）："
                                   "请到「账号授权」重新提交 Cookie", code="login_expired")
        for sel in self.cfg.get("login_selectors") or []:
            try:
                if page.locator(sel).first.count() > 0:
                    raise PublishError(f"[{self.name}] 登录态已失效（页面出现登录控件）："
                                       "请到「账号授权」重新提交 Cookie", code="login_expired")
            except PublishError:
                raise
            except Exception:  # noqa: BLE001
                continue
        # 顶部栏出现"登录"入口 = 没登录（58 这类不跳转、就地显示登录链接）
        for text in self.cfg.get("login_text") or []:
            try:
                hit = page.locator(f"a:visible:has-text('{text}'), "
                                   f"button:visible:has-text('{text}')").first
                if hit.count() > 0 and hit.inner_text().strip() == text:
                    raise PublishError(f"[{self.name}] 登录态已失效（页面显示「{text}」入口）："
                                       "请到「账号授权」重新提交 Cookie", code="login_expired")
            except PublishError:
                raise
            except Exception:  # noqa: BLE001
                continue

    def assert_no_captcha(self, page) -> None:
        """风控验证码要报 captcha（执行器据此走人工介入，§1.5 规则 4）。"""
        for sel in tuple(self.cfg.get("captcha_selectors") or ()) + _CAPTCHA_SELECTORS:
            try:
                if page.locator(sel).first.count() > 0:
                    raise PublishError(
                        f"[{self.name}] 平台弹出验证码/滑块：需人工处理。"
                        "用 `tools/publish_probe.py --headed` 打开浏览器人工过一次，"
                        "或改用蚁小二通道发布", code="captcha")
            except PublishError:
                raise
            except Exception:  # noqa: BLE001
                continue

    # ── 各步骤 ────────────────────────────────────────────────────────────
    def fill_title(self, page, title: str) -> None:
        loc = first_visible(page, self.cfg.get("title") or [])
        if loc is None:
            raise PublishError(self._missing("标题输入框"), code="unknown")
        loc.click()
        try:
            loc.fill(title)
        except Exception:  # noqa: BLE001 - contenteditable 标题框不支持 fill
            page.keyboard.insert_text(title)

    def fill_body(self, page, html: str) -> None:
        body_cfg = self.cfg.get("body") or {}
        scope = page
        if body_cfg.get("frame"):
            scope = page.frame_locator(body_cfg["frame"])
        loc = first_visible(page, body_cfg.get("selectors") or [], root=scope)
        if loc is None:
            raise PublishError(self._missing("正文编辑区"), code="unknown")
        loc.click()
        if body_cfg.get("mode") == "text":
            page.keyboard.insert_text(plain_text(html))
            return
        tag = self._tag_name(loc)
        if tag in ("TEXTAREA", "INPUT"):        # 表单型平台（列表网/58）没有富文本
            loc.fill(plain_text(html))
            return
        try:                      # 富文本编辑器通用做法：写 innerHTML + 派发 input
            loc.evaluate(
                "(el, html) => { el.innerHTML = html;"
                " el.dispatchEvent(new InputEvent('input', {bubbles: true})); }", html)
        except Exception:  # noqa: BLE001 - 编辑器拦截则退回纯文本粘贴
            page.keyboard.insert_text(plain_text(html))

    @staticmethod
    def _tag_name(loc) -> str:
        try:
            return str(loc.evaluate("el => el.tagName") or "").upper()
        except Exception:  # noqa: BLE001 - 取不到就当富文本容器
            return ""

    def upload_images(self, page, files) -> int:
        """尽力而为的配图上传（平台图床接口各不相同，失败只记日志不影响正文）。"""
        selectors = self.cfg.get("image_input") or []
        if not files or not selectors:
            return 0
        done = 0
        for path in files:
            try:
                page.locator(selectors[0]).first.set_input_files(str(path), timeout=8000)
                time.sleep(1.2)   # 等图床回写
                done += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("image_upload_skipped", extra={
                    "event": "image_upload_skipped", "platform": self.platform,
                    "err": str(exc)[:120]})
        return done

    def run_steps(self, page, steps) -> None:
        """表单型平台（列表网/58）的通用补步骤：
        `{"select": 选择器, "value": 选项文本}` / `{"fill": 选择器, "value": 文本}`
        / `{"click": 选择器}`。值写在 data/publish_selectors.json 里，便于现场校准。"""
        for step in steps:
            if not isinstance(step, dict):
                continue
            try:
                if step.get("select"):
                    page.locator(step["select"]).first.select_option(
                        label=str(step.get("value") or ""))
                elif step.get("fill"):
                    page.locator(step["fill"]).first.fill(str(step.get("value") or ""))
                elif step.get("click"):
                    page.locator(step["click"]).first.click()
                time.sleep(float(step.get("wait") or 0.5))
            except Exception as exc:  # noqa: BLE001
                raise PublishError(
                    f"[{self.name}] 表单步骤失败（{step}）：{str(exc)[:120]}",
                    code="unknown") from exc

    def click_submit(self, page) -> None:
        loc = first_visible(page, self.cfg.get("submit") or [])
        if loc is None:
            raise PublishError(self._missing("发布按钮"), code="unknown")
        loc.click()
        confirm = first_visible(page, self.cfg.get("confirm") or [], timeout=2000)
        if confirm is not None:
            try:
                confirm.click()
            except Exception:  # noqa: BLE001 - 弹窗可能已自动关闭
                pass

    def wait_success(self, page) -> str:
        """等成功判定（成功提示 / 跳转离开编辑器）；超时抛错，**不假设成功**。"""
        cfg = self.cfg.get("success") or {}
        editor_url = self.cfg.get("editor") or ""
        budget = float(cfg.get("timeout") or 45)
        deadline = time.time() + budget
        while time.time() < deadline:
            url = page.url or ""
            pattern = cfg.get("url_pattern")
            if pattern and url != editor_url and re.search(pattern, url):
                return url
            loc = first_visible(page, cfg.get("selectors") or [], timeout=800)
            if loc is not None:
                return url or editor_url
            if _text_present(page, cfg.get("text") or []):
                return url or editor_url
            page.wait_for_timeout(1000)
        raise PublishError(
            f"[{self.name}] 发布结果未确认（{int(budget)} 秒内既没有成功提示也没有跳转）："
            "可能被平台审核拦截或需要人工处理，请到平台后台确认后再重发", code="unknown")

    def _missing(self, what: str) -> str:
        return (f"[{self.name}] 找不到{what}：站点可能已改版。"
                "用 `python tools/publish_probe.py --platform "
                f"{self.platform}` 抓取当前页面元素，再把结果写进 "
                "data/publish_selectors.json 即可，无需改代码。")


def _text_present(page, texts) -> bool:
    for text in texts:
        try:
            if page.locator(f"text={text}").first.count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


__all__ = ["Adapter", "first_visible", "plain_text"]
