"""topic 蓝图 · 页面路由（只解析/渲染，不写业务规则、不直接查库）。"""
from __future__ import annotations

from flask import render_template

from app.blueprints.topic import bp


@bp.get("/titles", endpoint="titles_page")
def titles_page():
    return render_template("client_titles.html")
