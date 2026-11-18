"""百家号（baijiahao.baidu.com）浏览器适配器。

正文是 UEditor 内核，编辑区在 **iframe** 里（`#ueditor_0`）—— 写 iframe 的
body.innerHTML 而不是主文档。标题是 textarea。

选择器为初始值，改版后用 `tools/publish_probe.py --platform baijiahao` 校准。
"""
from __future__ import annotations

from app.services.publish.platforms.base import Adapter


class BaijiahaoAdapter(Adapter):
    platform = "baijiahao"
    name = "百家号"

    CONFIG = {
        "home": "https://baijiahao.baidu.com/",
        "editor": "https://baijiahao.baidu.com/builder/rc/edit?type=news",
        "login_url_patterns": ["passport.baidu.com", "/login"],
        "login_selectors": ["input[type='password']"],
        "title": ["textarea[placeholder*='标题']", "input[placeholder*='标题']",
                  "#newsTextarea", "textarea.title"],
        "body": {
            # UEditor：编辑区在 iframe 内（id 形如 ueditor_0）
            "frame": "iframe[id*='ueditor'], iframe.ueditor_editor, iframe[id*='editor']",
            "selectors": ["body.view", "body#ueditor_0", "body"],
            "mode": "html",
        },
        "image_input": ["input[type='file'][accept*='image']", "input[type='file']"],
        "submit": ["button:has-text('发布')", ".cheetah-btn-primary:has-text('发布')",
                   "text=发布"],
        "confirm": ["button:has-text('确定')", "button:has-text('确认')",
                    ".cheetah-modal button:has-text('确定')"],
        "success": {
            "text": ["发布成功", "已提交", "提交成功"],
            "url_pattern": r"baijiahao\.baidu\.com/builder/rc/(content|home|article)",
            "timeout": 45,
        },
    }
