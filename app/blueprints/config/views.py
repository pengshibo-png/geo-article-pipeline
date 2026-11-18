"""config 蓝图 · 页面路由（只解析/渲染，不写业务规则、不直接查库）。"""
from __future__ import annotations

from flask import render_template

from app.blueprints.config import bp


@bp.get("/brand", endpoint="brand_page")
def brand_page():
    return render_template("client_brand.html")


@bp.get("/cities", endpoint="cities_page")
def cities_page():
    return render_template("client_cities.html")


@bp.get("/audience", endpoint="audience_page")
def audience_page():
    return render_template("client_audience.html")


@bp.get("/prompts", endpoint="prompts_page")
def prompts_page():
    return render_template("client_prompts.html")


@bp.get("/assets", endpoint="assets_page")
def assets_page():
    return render_template("client_assets.html")
