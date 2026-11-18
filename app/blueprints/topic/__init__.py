"""topic 蓝图 —— 选题库 CRUD、AI 选题、同行抓取、文体配比。

**明确不做**：不写正文
"""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("topic", __name__)

from app.blueprints.topic import api, views  # noqa: E402,F401  注册路由
