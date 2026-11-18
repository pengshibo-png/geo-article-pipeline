"""美篇（www.meipian.cn）浏览器适配器。

规格优先级最高的一个（施工规格书：发布适配器 ×6「先美篇」）。
选择器为**初始值**：站点改版后跑 `tools/publish_probe.py --platform meipian`
抓取实际元素，把结果写进 `data/publish_selectors.json` 即可覆盖，不必改代码。
"""
from __future__ import annotations

from app.services.publish.platforms.base import Adapter


class MeipianAdapter(Adapter):
    platform = "meipian"
    name = "美篇"

    CONFIG = {
        "home": "https://www.meipian.cn/",
        # 写文章页（登录态由 Cookie 带入；未登录会被重定向到 /login）
        "editor": "https://www.meipian.cn/editor",
        "login_url_patterns": ["/login", "passport", "/signin"],
        "login_selectors": ["input[type='password']"],
        # 未登录时编辑器外壳照常渲染，但会盖一层登录弹窗（实测）
        "login_text": ["其他方式登录", "立即注册"],
        # 以下三条由 2026-10-14 实测 /editor 页面得到（编辑器外壳未登录也会渲染）
        "title": ["input.editor-title", "input[placeholder*='点击添加标题']",
                  "input[placeholder*='标题']"],
        "body": {
            "frame": None,
            # WangEditor 内核：可编辑区在 .w-e-text-container 里
            "selectors": [".w-e-text-container [contenteditable='true']", ".w-e-text",
                          ".ql-editor", "div[contenteditable='true']"],
            "mode": "html",
        },
        "image_input": ["input.mp-upload__input[type='file']",
                        "input[type='file'][accept*='image']", "input[type='file']"],
        "submit": ["button:has-text('完成/分享')", "button:has-text('发布')",
                   "button:has-text('完成')"],
        "confirm": ["button:has-text('确定')", "button:has-text('确认发布')",
                    "button:has-text('确认')"],
        "success": {
            "text": ["发布成功", "已发布", "发布完成"],
            "url_pattern": r"meipian\.cn/(?!editor)",
            "timeout": 45,
        },
    }
