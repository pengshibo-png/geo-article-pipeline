"""generation 蓝图 · JSON 接口（同 views 的职责，返回 JSON）。"""
from __future__ import annotations

from flask import jsonify, request

from app.blueprints.generation import bp
from app.blueprints.generation.service import GenService, ai_model_label


@bp.get("/api/generation/ping", endpoint="generation_ping")
def generation_ping():
    return jsonify({"ok": True, "blueprint": "generation"})


# ── 批量写稿（gen.html）──────────────────────────────────────────────────────

@bp.post("/api/run", endpoint="run_api")
def run_api():
    body = request.get_json(silent=True) or {}
    return jsonify(GenService().start_run(body))


@bp.post("/api/run/pause", endpoint="run_pause_api")
def run_pause_api():
    return jsonify(GenService().pause())


@bp.get("/api/status", endpoint="status_api")
def status_api():
    """layout.js 顶栏也读这个端点（只看 running），gen.html 读全量进度。"""
    try:
        lines = int(request.args.get("lines") or 200)
    except (TypeError, ValueError):
        lines = 200
    return jsonify(GenService().status(lines))


# ── 顶栏模型标签（layout.js refreshHeader）──────────────────────────────────

@bp.get("/api/config", endpoint="ai_config_api")
def ai_config_api():
    return jsonify({"ai": {"model": ai_model_label()}})
