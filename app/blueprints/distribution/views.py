"""distribution 蓝图 · 页面路由（只解析/渲染，不写业务规则、不直接查库）。"""
from __future__ import annotations

from flask import render_template

from app.blueprints.distribution import bp


@bp.get("/tasks", endpoint="tasks_page")
def tasks_page():
    return render_template("client_tasks.html")


@bp.get("/archives", endpoint="archives_page")
def archives_page():
    return render_template("client_archives.html")


@bp.get("/publog", endpoint="publog_page")
def publog_page():
    return render_template("client_publog.html")


@bp.get("/auth", endpoint="myaccounts_page")
def myaccounts_page():
    return render_template("client_myaccounts.html")
