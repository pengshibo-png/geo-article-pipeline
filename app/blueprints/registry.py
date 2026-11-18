"""蓝图注册表：集中列出，供 create_app 遍历注册。"""
from __future__ import annotations

from app.blueprints.auth import bp as auth_bp
from app.blueprints.config import bp as config_bp
from app.blueprints.console import bp as console_bp
from app.blueprints.distribution import bp as distribution_bp
from app.blueprints.generation import bp as generation_bp
from app.blueprints.insight import bp as insight_bp
from app.blueprints.topic import bp as topic_bp

BLUEPRINTS = (auth_bp, console_bp, config_bp, topic_bp, generation_bp,
              distribution_bp, insight_bp)
