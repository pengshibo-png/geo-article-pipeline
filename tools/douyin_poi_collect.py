"""抖音地点（POI）半自动采集工具。

背景：蚁小二 `query locations` 的搜索被"出口地区"锁死（上海账号搜外地只回上海地点），
代理又没有江浙沪城市出口。所以只能像人一样：用**已登录的抖音网页**搜地点、读结果。

流程：
1. 打开一个**可见**的 Chromium（持久化 profile，登录一次后长期有效）；
2. 你手动登录抖音 + 过验证码；
3. 脚本检测到登录后，逐个地区搜索并导出「地区 → 地点名 → 分享链接/POI ID」清单。

用法：
    python tools/douyin_poi_collect.py diag       # 只搜 3 个样例，dump 页面结构
    python tools/douyin_poi_collect.py collect    # 全量采集（198 区县）

产物：
    data/local/douyin/poi_manifest.json          采集清单
    data/local/douyin/poi_diag/                  诊断快照（html/txt/png）
    data/local/douyin/poi_collect_status.json    运行状态（页面轮询用）
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from desktop.paths import pin_environment  # noqa: E402

pin_environment()

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "local" / "douyin"
DIAG = DATA / "poi_diag"
STATUS = DATA / "poi_collect_status.json"
MANIFEST = DATA / "poi_manifest.json"
PROFILE = DATA / "browser_profile"

# 浏览器可执行文件：默认走 Playwright 自带 Chromium（`python -m playwright install chromium`）；
# 如需指到本机已下载的其他内核，用环境变量覆盖，如
#   set DOUYIN_CHROME_EXE=%LOCALAPPDATA%\ms-playwright\chromium-1248\chrome-win64\chrome.exe
EXE = os.environ.get("DOUYIN_CHROME_EXE") or None

DIAG_KEYWORDS = ["宁波天一广场", "鄞州万达广场", "莫干山风景区"]

POI_RE = re.compile(r"poi_id[=:\"]{1,3}(\d{6,})")


def say(*a) -> None:
    print(*a, flush=True)


def status(**kw) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(kw, ensure_ascii=False, indent=1), encoding="utf-8")


def regions() -> list[str]:
    """业务城市里的区/县（排除地级市名）。"""
    from app import create_app
    from app.services.douyin_pack import _PREFECTURE_CITIES
    from app.models import TitleConfig
    from app.tenancy import TenantScopedRepository, tenant_scope

    app = create_app("dev")
    with app.app_context(), tenant_scope("local"):
        tc = TenantScopedRepository(TitleConfig).first()
        raw = (tc.cities if tc else "") or ""
        names = [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]
    return [n for n in names if n not in _PREFECTURE_CITIES]


def wait_login(ctx, timeout: int = 900) -> bool:
    """轮询 cookie，等你在浏览器里登录抖音（sessionid 出现即视为登录）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        names = {c.get("name") for c in ctx.cookies()}
        if {"sessionid", "sessionid_ss"} & names:
            return True
        time.sleep(1)
    return False


def dump(page, tag: str) -> dict:
    DIAG.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", tag)[:40]
    html = page.content()
    (DIAG / f"{safe}.html").write_text(html, encoding="utf-8")
    links = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => [e.href, (e.innerText||'').trim().slice(0,80)])")
    poi_links = [l for l in links
                 if "/poi/" in l[0] or "poi_id" in l[0]]
    ids = sorted(set(POI_RE.findall(html)))
    info = {"tag": tag, "url": page.url, "title": page.title(),
            "links": len(links), "poi_links": poi_links[:20],
            "poi_ids_in_html": ids[:20],
            "body": page.inner_text("body")[:1500]}
    (DIAG / f"{safe}.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        page.screenshot(path=str(DIAG / f"{safe}.png"), full_page=False)
    except Exception:  # noqa: BLE001
        pass
    return info


def run(mode: str) -> None:
    from playwright.sync_api import sync_playwright

    DATA.mkdir(parents=True, exist_ok=True)
    status(state="starting", message="正在打开浏览器…")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=False,
            executable_path=EXE, locale="zh-CN",
            viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.douyin.com/", timeout=60000)
        status(state="waiting_login",
               message="请在打开的浏览器里登录抖音并完成验证码；登录后脚本会自动继续")
        say("请在浏览器里登录抖音；检测到登录后自动继续…")
        if not wait_login(ctx):
            status(state="login_timeout", message="等待登录超时")
            say("等待登录超时")
            ctx.close()
            return
        status(state="logged_in", message="已登录，开始搜索")
        say("已登录，开始搜索")

        targets = DIAG_KEYWORDS if mode == "diag" else regions()
        total = len(targets)
        manifest: dict = {}
        for i, kw in enumerate(targets, 1):
            status(state="running", done=i - 1, total=total, current=kw)
            url = "https://www.douyin.com/search/" + urllib.parse.quote(kw)
            try:
                page.goto(url, timeout=60000)
                page.wait_for_timeout(3500)
                info = dump(page, kw)
                if mode == "diag":
                    say(f"[{i}/{total}] {kw}: poi_links={len(info['poi_links'])} "
                        f"poi_ids={info['poi_ids_in_html'][:3]} body={info['body'][:80]!r}")
                else:
                    manifest[kw] = {"poi_links": info["poi_links"],
                                    "poi_ids": info["poi_ids_in_html"]}
                    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
                    say(f"[{i}/{total}] {kw}: 链接 {len(info['poi_links'])} / ID {len(info['poi_ids_in_html'])}")
            except Exception as exc:  # noqa: BLE001
                say(f"[{i}/{total}] {kw} 失败: {str(exc)[:120]}")
            time.sleep(1.2)

        status(state="done", done=total, total=total)
        say("完成")
        ctx.close()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "diag")
