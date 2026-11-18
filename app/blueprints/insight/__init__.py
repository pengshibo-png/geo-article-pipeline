"""insight 蓝图 —— GEO 采样、引用归因、报告生成与导出、竞品对比。

**明确不做**：不改内容
"""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("insight", __name__)

from app.blueprints.insight import api, views  # noqa: E402,F401  注册路由
