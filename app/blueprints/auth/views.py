"""auth 蓝图 · 页面路由（只解析/渲染，不写业务规则、不直接查库）。"""
from __future__ import annotations

from flask import render_template

from app.blueprints.auth import bp


@bp.get("/login", endpoint="login_page")
def login_page():
    return render_template("login.html")


@bp.get("/register", endpoint="register_page")
def register_page():
    return render_template("register.html")


@bp.get("/tenants", endpoint="tenants_page")
def tenants_page():
    return render_template("client_tenants.html")


@bp.get("/settings", endpoint="settings_page")
def settings_page():
    return render_template("client_settings.html")
