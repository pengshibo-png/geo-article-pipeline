"""WSGI 入口。**只做两件事**：装配应用、暴露入口（ADR §1.5：≤ 60 行）。

开发：  ./.venv/Scripts/python.exe app.py
生产：  gunicorn "app:create_app()" -w 4 -b 0.0.0.0:8000   ← 禁用 Werkzeug（ADR-012）
"""
from __future__ import annotations

import os

from app import create_app

app = create_app(os.environ.get("GEO_ENV"))


def _init_db() -> None:
    """本地开发用：建表（生产一律用 Alembic 迁移）。"""
    from app.extensions import db

    with app.app_context():
        db.create_all()
        print("数据库表已创建（开发模式；生产请用 alembic upgrade head）")


if __name__ == "__main__":
    if os.environ.get("GEO_INIT_DB") == "1":
        _init_db()
    app.run(host="127.0.0.1", port=int(os.environ.get("GEO_PORT", "7001")), debug=True)
