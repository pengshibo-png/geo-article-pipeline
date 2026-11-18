"""搜狐号（mp.sohu.com）浏览器适配器。

搜狐号编辑器是 Quill 内核（`.ql-editor`），正文可直接写 innerHTML；
发布后进入内容管理列表页 —— 以「离开 addarticle 页」作为成功判定。

选择器为初始值，改版后用 `tools/publish_probe.py --platform souhu` 校准。
"""
from __future__ import annotations

from app.services.publish.platforms.base import Adapter


class SouhuAdapter(Adapter):
    platform = "souhu"
    name = "搜狐号"

    CONFIG = {
        "home": "https://mp.sohu.com/",
        "editor": "https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle",
        "login_url_patterns": ["passport.sohu.com", "/login", "login.sohu.com"],
        "login_selectors": ["input[type='password']"],
        "title": ["input[placeholder*='请输入标题']", "input[placeholder*='标题']",
                  "textarea[placeholder*='标题']", "#title"],
        "body": {
            "frame": None,
            "selectors": [".ql-editor", "div[contenteditable='true']", "#editor"],
            "mode": "html",
        },
        "image_input": ["input[type='file'][accept*='image']", "input[type='file']"],
        "submit": ["button:has-text('发布')", ".btn-publish", "text=发布"],
        "confirm": ["button:has-text('确定')", "button:has-text('确认')",
                    ".el-message-box__btns button:has-text('确定')"],
        "success": {
            "text": ["发布成功", "提交成功"],
            # 发布成功后回到内容管理列表（不再是 addarticle 编辑页）
            "url_pattern": r"mp\.sohu\.com/mpfe/v4/contentManagement/news(?!.*addarticle)",
            "selectors": [".el-message--success"],
            "timeout": 45,
        },
    }
