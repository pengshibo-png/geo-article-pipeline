"""generation 蓝图 —— A/B/C 流水线、抖音图文、视频产线。

**明确不做**：不直接发布
"""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("generation", __name__)

from app.blueprints.generation import api, api_douyin, api_video, views  # noqa: E402,F401  注册路由
