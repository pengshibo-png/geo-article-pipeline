"""generation 蓝图 · 页面路由（只解析/渲染，不写业务规则、不直接查库）。"""
from __future__ import annotations

from flask import render_template

from app.blueprints.generation import bp


@bp.get("/gen", endpoint="gen_page")
def gen_page():
    return render_template("client_gen.html")


@bp.get("/douyin", endpoint="douyin_page")
def douyin_page():
    return render_template("client_douyin.html")


@bp.get("/video", endpoint="video_page")
def video_page():
    return render_template("client_video.html")
