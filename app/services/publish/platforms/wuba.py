"""58同城（post.58.com）浏览器适配器。

58 的发布链路：选城市 → 选大类/小类 → 填标题/描述 → 手机号验证 → 提交，
风控最严（常见滑块/验证码 → 归一化成 captcha，账号进人工介入通道）。

⚠ 分类路径与验证环节强依赖站点当前结构，**必须用真号现场校准**
（`tools/publish_probe.py --platform wuba --headed`），校准结果写
`data/publish_selectors.json` 的 extra_steps 里，不必改代码。
"""
from __future__ import annotations

from app.services.publish.platforms.base import Adapter


class WubaAdapter(Adapter):
    platform = "wuba"
    name = "58同城"

    CONFIG = {
        "home": "https://www.58.com/",
        "editor": "https://post.58.com/",
        "login_url_patterns": ["passport.58.com", "login.58.com", "/login"],
        "login_selectors": ["input[type='password']"],
        # 58 不跳登录页：顶部栏就地显示「登录」入口（实测 post.58.com/342）
        "login_text": ["登录", "立即登录"],
        "title": ["input[placeholder*='标题']", "input[name*='title']",
                  "input[placeholder*='简要']"],
        "body": {
            "frame": None,
            "selectors": ["textarea[placeholder*='描述']", "textarea[name*='content']",
                          "textarea", "div[contenteditable='true']"],
            "mode": "html",
        },
        "image_input": ["input[type='file'][accept*='image']", "input[type='file']"],
        "submit": ["button:has-text('发布')", "button:has-text('立即发布')",
                   "input[type='submit']", "text=发布"],
        "confirm": ["button:has-text('确定')", "button:has-text('确认')"],
        "success": {
            # 不用 URL 判定：post.58.com 会自己跳城市页（/342），URL 判定会误报成功
            "text": ["发布成功", "提交成功", "信息发布成功", "审核中"],
            "timeout": 60,
        },
        # 现场校准用：城市/分类级联选择（示例，默认留空不执行）
        "extra_steps": [],
    }
