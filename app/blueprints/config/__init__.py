"""config 蓝图 —— 品牌资料/城市/画像/指令/素材（业务事实源）。

**明确不做**：不触发生成
"""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("config", __name__)

from app.blueprints.config import api, views  # noqa: E402,F401  注册路由
