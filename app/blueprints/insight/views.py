"""insight 蓝图 · 页面路由（只解析/渲染，不写业务规则、不直接查库）。"""
from __future__ import annotations

from flask import render_template

from app.blueprints.insight import bp


@bp.get("/geo", endpoint="geo_page")
def geo_page():
    return render_template("client_geo.html")


@bp.get("/citations", endpoint="citations_page")
def citations_page():
    return render_template("client_citations.html")


@bp.get("/reports", endpoint="reports_page")
def reports_page():
    return render_template("client_reports.html")


@bp.get("/competitor", endpoint="competitor_page")
def competitor_page():
    return render_template("client_competitor.html")
