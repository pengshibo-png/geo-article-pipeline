"""generation 蓝图 · AI 短视频产线路由（/api/video/*）。

薄路由层：解析请求 → VideoService / video_store → JSON。业务在 service/jobs。
"""
from __future__ import annotations

from pathlib import Path

from flask import Response, jsonify, request

from app.blueprints.generation import bp
from app.blueprints.generation.video_service import VideoService
from app.services import video_store


def _payload() -> dict:
    return request.get_json(silent=True) or {}


@bp.get("/api/video/list", endpoint="video_list_api")
def video_list_api():
    return jsonify(VideoService().list_videos())


@bp.post("/api/video/generate", endpoint="video_generate_api")
def video_generate_api():
    body, code = VideoService().start_generate()
    return jsonify(body), code


@bp.post("/api/video/publish", endpoint="video_publish_api")
def video_publish_api():
    body, code = VideoService().publish(str(_payload().get("id") or ""))
    return jsonify(body), code


@bp.post("/api/video/delete", endpoint="video_delete_api")
def video_delete_api():
    return jsonify(VideoService().delete(str(_payload().get("id") or "")))


@bp.get("/api/video/sched", endpoint="video_sched_api")
def video_sched_api():
    return jsonify(VideoService().autopub_get())


@bp.post("/api/video/sched", endpoint="video_sched_save_api")
def video_sched_save_api():
    return jsonify(VideoService().autopub_save(_payload()))


@bp.get("/api/video/file/<video_id>.mp4", endpoint="video_file_api")
def video_file_api(video_id: str):
    """成片字节服务（封面帧由前端 `#t=0.5` 解出，不必单独存封面文件服务）。"""
    try:
        path = video_store.video_path(video_id)
    except Exception:                            # noqa: BLE001 - 非法编号统一 404
        return jsonify({"ok": False, "error": "视频不存在"}), 404
    if not Path(path).exists():
        return jsonify({"ok": False, "error": "成片尚未生成"}), 404
    return Response(path.read_bytes(), mimetype="video/mp4")
