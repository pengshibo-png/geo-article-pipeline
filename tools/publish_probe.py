#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布渠道「现场校准」工具（真实浏览器驱动专用，不参与线上运行）。

平台改版时选择器会失效。本工具用真账号 Cookie 打开编辑器页面，把页面上
**真实存在**的输入框/编辑区/按钮列出来，并给出可直接粘贴的选择器建议；
`--publish` 还能拿一篇样例文章真跑一次发布流程，把卡点与失败截图指出来。

用法（Windows 源码态）：
    ./.venv/Scripts/python.exe tools/publish_probe.py --platform meipian \\
        --account mp-1a2b3c4d5e6f --headed
    ./.venv/Scripts/python.exe tools/publish_probe.py --platform souhu \\
        --cookie "a=1; b=2" --publish --title "测试标题"
    # 建议写进覆盖文件（不用改代码）：
    ./.venv/Scripts/python.exe tools/publish_probe.py --platform meipian \\
        --account mp-xxx --write

校准结果落到 `data/publish_selectors.json`（形如 {"meipian": {"title": [...]}}），
运行时自动覆盖代码里的默认选择器。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app                                            # noqa: E402
from app.services.publish import browser                              # noqa: E402
from app.services.publish.platforms import ADAPTERS, get_adapter      # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_DUMP_JS = """
() => {
  const out = [];
  const vis = el => { const r = el.getBoundingClientRect();
                      return r.width > 0 && r.height > 0; };
  document.querySelectorAll("input, textarea, [contenteditable='true'], button, [role=button], a")
    .forEach(el => out.push({
      tag: el.tagName, type: el.type || "", id: el.id || "",
      name: el.name || "", cls: String(el.className || "").slice(0, 60),
      ph: el.placeholder || "", text: (el.innerText || "").trim().slice(0, 30),
      visible: vis(el)}));
  const frames = [...document.querySelectorAll("iframe")].map(f => ({
      id: f.id || "", cls: String(f.className || "").slice(0, 40), src: (f.src || "").slice(0, 80)}));
  return {items: out, frames, url: location.href, title: document.title};
}
"""


def _load_cookie(account_id: str, raw: str | None) -> tuple[str, str]:
    """返回 (cookie 原文, 租户 id)。account_id 走库（解密），raw 直接给。"""
    if raw:
        return raw, ""
    app = create_app("dev")
    with app.app_context():
        from app.extensions import db
        from app.models import Account
        from app.services.crypto import decrypt_text

        row = db.session.query(Account).filter(Account.id == account_id).first()
        if row is None:
            raise SystemExit(f"账号不存在：{account_id}（先到 /auth 页添加）")
        return decrypt_text(row.cookie_blob), row.tenant_id or ""


def _suggest(dump: dict, adapter) -> dict:
    """从页面元素里挑出可用选择器建议（按优先级给多个候选）。"""
    items = [i for i in dump["items"] if i["visible"]]
    title, body, submit = [], [], []
    for i in items:
        if i["tag"] in ("INPUT", "TEXTAREA") and ("标题" in i["ph"] or "title" in i["name"].lower()):
            title.append(f'{i["tag"].lower()}[placeholder*="{i["ph"][:12]}"]' if i["ph"]
                         else f'{i["tag"].lower()}[name="{i["name"]}"]')
        if i["ph"] and ("描述" in i["ph"] or "正文" in i["ph"] or "内容" in i["ph"]):
            body.append(f'{i["tag"].lower()}[placeholder*="{i["ph"][:10]}"]')
    for i in items:
        if i["tag"] == "DIV" and i["id"]:
            body.append(f'#{i["id"]}')
        if i["cls"] and ("ql-editor" in i["cls"] or "editor" in i["cls"].lower()):
            body.append(f'.{i["cls"].split()[0]}')
        if i["tag"] == "BUTTON" and any(k in i["text"] for k in ("发布", "提交", "完成", "保存")):
            submit.append(f'button:has-text("{i["text"][:6]}")')
    out = {}
    if title:
        out["title"] = list(dict.fromkeys(title))
    if body:
        out["body"] = {**(adapter.cfg.get("body") or {}), "selectors": list(dict.fromkeys(body))}
    if submit:
        out["submit"] = list(dict.fromkeys(submit))
    real_frames = [f for f in dump["frames"] if not _is_captcha_frame(f)]
    if real_frames:
        f = real_frames[0]
        if f["id"]:
            out["body"] = {**(out.get("body") or adapter.cfg.get("body") or {}),
                           "frame": f'iframe#{f["id"]}', "selectors": ["body"]}
    return out


_CAPTCHA_HINT = ("captcha", "tcaptcha", "verify", "geetest", "yidun", "nc_1")


def _is_captcha_frame(frame: dict) -> bool:
    blob = f"{frame.get('id', '')} {frame.get('cls', '')} {frame.get('src', '')}".lower()
    return any(h in blob for h in _CAPTCHA_HINT)


def _print_dump(dump: dict) -> None:
    print(f"\n页面标题：{dump['title']}\n当前地址：{dump['url']}\n")
    print(f"{'标签':<8}{'可见':<6}{'占位/文本':<24}{'id / class'}")
    print("-" * 90)
    for i in dump["items"]:
        hint = i["ph"] or i["text"]
        print(f"{i['tag']:<8}{'✓' if i['visible'] else '·':<6}{hint[:22]:<24}"
              f"{(i['id'] or i['cls'])[:40]}")
    if dump["frames"]:
        print("\niframe：")
        for f in dump["frames"]:
            print(f"  id={f['id']!r} cls={f['cls']!r} src={f['src']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="发布渠道现场校准")
    ap.add_argument("--platform", required=True, choices=sorted(ADAPTERS))
    ap.add_argument("--account", help="库里的账号 id（自动解密 Cookie）")
    ap.add_argument("--cookie", help="直接给 Cookie 串（不想入库时用）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（推荐首次校准）")
    ap.add_argument("--publish", action="store_true", help="真跑一次发布流程")
    ap.add_argument("--title", default="发布链路自检（可删除）")
    ap.add_argument("--html", help="正文 HTML 文件路径（默认用一段测试正文）")
    ap.add_argument("--click", help="打开页面后先点一下（分类信息站要先选分类才出表单），"
                                    "可重复：--click \"a:has-text('家政')\"")
    ap.add_argument("--url", help="覆盖编辑器地址（站点改版时试新地址）")
    ap.add_argument("--write", action="store_true", help="把建议写入 data/publish_selectors.json")
    args = ap.parse_args()

    if not args.account and not args.cookie:
        raise SystemExit("必须给 --account 或 --cookie")
    raw_cookie, _tid = _load_cookie(args.account, args.cookie)
    adapter = get_adapter(args.platform)
    domain = browser.cookie_domain(adapter.cfg["home"])
    cookies = browser.parse_cookies(raw_cookie, domain=domain)
    print(f"平台：{adapter.name}（{args.platform}）  Cookie 域：{domain}  条数：{len(cookies)}")
    if not cookies:
        raise SystemExit("Cookie 解析为空：请检查粘贴内容")

    html = Path(args.html).read_text(encoding="utf-8") if args.html else \
        "<p>这是一篇用于自检发布链路的测试正文，可直接删除。</p>"
    if args.url:
        adapter.cfg["editor"] = args.url
    with browser.BrowserSession(platform=args.platform, home=adapter.cfg["home"],
                                cookies=cookies, headless=not args.headed) as page:
        page.goto(adapter.cfg["editor"], wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        if args.click:
            try:
                page.locator(args.click).first.click()
                page.wait_for_timeout(2000)
                print(f"已点击：{args.click}")
            except Exception as exc:  # noqa: BLE001
                print(f"点击失败（{args.click}）：{str(exc).splitlines()[0][:120]}")
        dump = page.evaluate(_DUMP_JS)
        _print_dump(dump)
        try:
            adapter.assert_logged_in(page)
            print("\n登录态：✅ 正常（没被跳到登录页）")
        except Exception as exc:  # noqa: BLE001
            print(f"\n登录态：❌ {exc}\n→ 请重新在浏览器里登录该平台，再把新的 Cookie 提交到「账号授权」")
        try:
            adapter.assert_no_captcha(page)
        except Exception as exc:  # noqa: BLE001
            print(f"\n风控：⚠ {exc}")
        if not [i for i in dump["items"] if i["visible"] and i["tag"] in ("INPUT", "TEXTAREA", "DIV")]:
            print("提示：页面上没有可见的输入控件 —— 若是分类信息站，先用 --click 选到分类再看表单")
        suggestion = _suggest(dump, adapter)
        print("\n建议选择器（可写入 data/publish_selectors.json）：")
        print(json.dumps({args.platform: suggestion}, ensure_ascii=False, indent=2))
        if args.write:
            path = Path(browser._data_root()) / "publish_selectors.json"  # noqa: SLF001
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            data[args.platform] = {**data.get(args.platform, {}), **suggestion}
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.reset_selector_cache()
            print(f"\n已写入：{path}")
        if args.publish:
            print("\n开始真跑发布流程 …")
            try:
                url = adapter.publish(page, html=html, title=args.title)
                print(f"✅ 发布成功：{url}")
            except Exception as exc:  # noqa: BLE001
                print(f"❌ 发布失败：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
