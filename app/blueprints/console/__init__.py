"""console 蓝图 —— 首页聚合 4 个域的只读摘要。

**明确不做**：不产生任何写操作
"""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("console", __name__)

from app.blueprints.console import api, views  # noqa: E402,F401  注册路由
