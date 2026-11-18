"""列表网（www.lieju.com）浏览器适配器。

列表网是**分类信息表单**（不是富文本编辑器）：标题 + 正文多为 textarea，
且必须先选城市/分类才能提交 —— 这类步骤写在 `extra_steps` 里，值可以放进
`data/publish_selectors.json` 现场调整（例如分类选「生活服务」）。

⚠ 分类/城市的选项文本与 URL 因站点改版可能变化；本适配器需**首次接真号时校准**。
"""
from __future__ import annotations

from app.services.publish.platforms.base import Adapter


class LiejuAdapter(Adapter):
    platform = "lieju"
    name = "列表网"

    CONFIG = {
        "home": "https://www.lieju.com/",
        "editor": "https://www.lieju.com/post/",
        "login_url_patterns": ["/login", "/passport", "login.lieju.com"],
        "login_selectors": ["input[type='password']"],
        "title": ["input[placeholder*='标题']", "input[name*='title']",
                  "input[placeholder*='标题']"],
        "body": {
            "frame": None,
            # mode=html：命中 textarea 走 fill（长文本更稳），命中 contenteditable 走 innerHTML
            "selectors": ["textarea[placeholder*='描述']", "textarea[name*='content']",
                          "textarea", ".ql-editor", "div[contenteditable='true']"],
            "mode": "html",
        },
        "image_input": ["input[type='file'][accept*='image']", "input[type='file']"],
        "submit": ["button:has-text('发布')", "button:has-text('立即发布')",
                   "input[type='submit']", "text=发布"],
        "confirm": ["button:has-text('确定')", "button:has-text('确认')"],
        "success": {
            # 列表网发布后仍停在 /post 下，URL 判定会误报；只认成功文案
            "text": ["发布成功", "提交成功", "信息发布成功", "审核"],
            "timeout": 45,
        },
        # 现场校准用：先选分类/城市再填正文（示例，默认留空不执行）
        "extra_steps": [],
    }
