"""generation 蓝图 · 抖音图文产线路由（/api/douyin/* 与 /media/douyin/*）。

薄路由层：解析请求 → DouyinService / douyin_jobs → JSON。业务在 service/jobs。
"""
from __future__ import annotations

from flask import Response, jsonify, request

from app.blueprints.generation import bp
from app.blueprints.generation.douyin_poi_service import (
    poi_override_list, poi_override_set, poi_pool_detail, poi_pool_refresh,
    poi_pool_status, poi_preview)
from app.blueprints.generation.douyin_service import DouyinService
from app.services import douyin_store

_MIME = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg",
         "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def _payload() -> dict:
    return request.get_json(silent=True) or {}


@bp.get("/api/douyin/config", endpoint="douyin_config_api")
def douyin_config_api():
    return jsonify(DouyinService().get_config())


@bp.post("/api/douyin/config", endpoint="douyin_config_save_api")
def douyin_config_save_api():
    return jsonify(DouyinService().save_config(_payload()))


@bp.get("/api/douyin/image-providers", endpoint="douyin_image_providers_api")
def douyin_image_providers_api():
    return jsonify(DouyinService().image_providers())


@bp.get("/api/douyin/posts", endpoint="douyin_posts_api")
def douyin_posts_api():
    return jsonify(DouyinService().posts())


@bp.get("/api/douyin/ledger", endpoint="douyin_ledger_api")
def douyin_ledger_api():
    return jsonify(DouyinService().ledger())


@bp.get("/api/douyin/status", endpoint="douyin_status_api")
def douyin_status_api():
    return jsonify(DouyinService().status())


@bp.get("/api/douyin/accounts", endpoint="douyin_accounts_api")
def douyin_accounts_api():
    return jsonify(DouyinService().accounts())


@bp.post("/api/douyin/generate", endpoint="douyin_generate_api")
def douyin_generate_api():
    # 库存上限闸门：只有"单次 count≤20"是不够的 —— 循环发布自动补产 + 反复手点
    # 可以无限堆积（实测 46 个内容包已占 469MB）。到顶就拒绝，让用户先发布或清理。
    from app.services import douyin_store

    cfg = DouyinService().get_config()
    cap = int(cfg.get("max_stock") or 0)
    if cap:
        stock = len(douyin_store.list_packs())
        if stock >= cap:
            return jsonify({"ok": False,
                            "error": f"内容包库存已达上限 {cap} 个（当前 {stock} 个）；"
                                     "请先发布或清理旧内容包，再生成新的"}), 409
    body, code = DouyinService().start_generate(_payload())
    return jsonify(body), code


@bp.post("/api/douyin/generate/stop", endpoint="douyin_generate_stop_api")
def douyin_generate_stop_api():
    return jsonify(DouyinService().stop_generate())


@bp.post("/api/douyin/publish", endpoint="douyin_publish_api")
def douyin_publish_api():
    body, code = DouyinService().publish(_payload())
    return jsonify(body), code


@bp.post("/api/douyin/publish/preview", endpoint="douyin_publish_preview_api")
def douyin_publish_preview_api():
    # 发布弹窗里的"将发出 N 条"由服务端算（与真跑同一份 plan），前端不再自己估。
    return jsonify(DouyinService().preview_publish(_payload()))


@bp.post("/api/douyin/publish/pause", endpoint="douyin_publish_pause_api")
def douyin_publish_pause_api():
    return jsonify(DouyinService().publish_pause())


@bp.post("/api/douyin/publish/resume", endpoint="douyin_publish_resume_api")
def douyin_publish_resume_api():
    return jsonify(DouyinService().publish_resume())


@bp.post("/api/douyin/timed", endpoint="douyin_timed_set_api")
def douyin_timed_set_api():
    from app.errors import NotFound
    from app.services import douyin_timed

    body = _payload()
    pack_id = body.get("pack_id")
    at_raw = str(body.get("at") or "").strip()
    if at_raw and not douyin_timed.norm_at(at_raw):
        return jsonify({"ok": False,
                        "error": f"定时时间无法识别：{at_raw[:24]}"}), 400
    try:
        at = douyin_timed.set_at(pack_id, at_raw)
    except NotFound as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "publish_at": at})


@bp.get("/api/douyin/poi-pool", endpoint="douyin_poi_pool_detail_api")
def douyin_poi_pool_detail_api():
    return jsonify(poi_pool_detail())


@bp.get("/api/douyin/poi-pool/status", endpoint="douyin_poi_pool_status_api")
def douyin_poi_pool_status_api():
    return jsonify(poi_pool_status())


@bp.post("/api/douyin/poi-pool/preview", endpoint="douyin_poi_preview_api")
def douyin_poi_preview_api():
    return jsonify(poi_preview(_payload()))


@bp.get("/api/douyin/poi-override", endpoint="douyin_poi_override_list_api")
def douyin_poi_override_list_api():
    return jsonify(poi_override_list(request.args.get("pack_id") or ""))


@bp.post("/api/douyin/poi-override", endpoint="douyin_poi_override_set_api")
def douyin_poi_override_set_api():
    return jsonify(poi_override_set(_payload()))


@bp.post("/api/douyin/poi-pool/refresh", endpoint="douyin_poi_pool_refresh_api")
def douyin_poi_pool_refresh_api():
    body, code = poi_pool_refresh(_payload())
    return jsonify(body), code


@bp.post("/api/douyin/pack/save", endpoint="douyin_pack_save_api")
def douyin_pack_save_api():
    return jsonify(DouyinService().pack_save(_payload()))


@bp.post("/api/douyin/pack/delete", endpoint="douyin_pack_delete_api")
def douyin_pack_delete_api():
    return jsonify(DouyinService().pack_delete(_payload()))


@bp.get("/api/douyin/autopub", endpoint="douyin_autopub_api")
def douyin_autopub_api():
    return jsonify(DouyinService().autopub_get())


@bp.post("/api/douyin/autopub", endpoint="douyin_autopub_save_api")
def douyin_autopub_save_api():
    return jsonify(DouyinService().autopub_save(_payload()))


@bp.get("/media/douyin/<pack_id>/<filename>", endpoint="douyin_media_api")
def douyin_media_api(pack_id: str, filename: str):
    data = douyin_store.read_image(pack_id, filename)
    ext = filename.rsplit(".", 1)[-1].lower()
    return Response(data, status=200, mimetype=_MIME.get(ext, "application/octet-stream"))
