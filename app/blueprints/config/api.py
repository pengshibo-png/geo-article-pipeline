"""config 蓝图 · JSON 接口（同 views 的职责，返回 JSON）。

契约对齐参考系统的线上实测：
- `GET  /api/brand`    → `{content}`（原系统仅此一个字段）
- `POST /api/brand`    → 整份保存
- `GET/POST /api/cities`、`/api/audience`同理。

**增强**：响应在`content` 之外挂完整度派生字段（completeness / sections / derived）。
前端可直接渲染完整度环，无需二次计算；旧字段一个没动，故向后兼容。
"""
from __future__ import annotations

from flask import Response, jsonify, request

from app.blueprints.config import bp
from app.blueprints.config.assets_service import AssetsService
from app.blueprints.config.model_service import ModelSettingsService
from app.blueprints.config.prompt_service import PromptService
from app.blueprints.config.service import ConfigService


def _payload() -> dict:
    return request.get_json(silent=True) or {}


@bp.get("/api/config/ping", endpoint="config_ping")
def config_ping():
    return jsonify({"ok": True, "blueprint": "config"})


# ── 品牌资料 ────────────────────────────────────────────────────────────────

@bp.get("/api/brand", endpoint="brand_api")
def brand_api():
    return jsonify(ConfigService().get_brand())


@bp.post("/api/brand", endpoint="brand_save_api")
def brand_save_api():
    return jsonify(ConfigService().save_brand(_payload().get("content", "")))


@bp.get("/api/brand/blockers", endpoint="brand_blockers_api")
def brand_blockers_api():
    """生成前的阻塞/警告清单。前端据此决定"能否开始生成"。"""
    items = ConfigService().brand_blockers()
    return jsonify({"items": items, "blocking": any(i["level"] == "block" for i in items)})


# ── 业务城市 ────────────────────────────────────────────────────────────────

@bp.get("/api/cities", endpoint="cities_api")
def cities_api():
    """双形状：管理端读 `items`，原站客户页读 `cities/ratio/count`。"""
    return jsonify(ConfigService().get_city_config())


@bp.post("/api/cities", endpoint="cities_add_api")
def cities_add_api():
    """双形状提交：`cities`（名字列表）= 客户页整份同步；`names` = 管理页批量添加。"""
    body = _payload()
    if isinstance(body.get("cities"), list):
        ratio = int(body.get("ratio") or 60)
        return jsonify(ConfigService().sync_cities([str(x) for x in body["cities"]], ratio))
    names = body.get("names")
    if isinstance(names, str):
        names = [ln.strip() for ln in names.replace("，", ",").split("\n") if ln.strip()]
    return jsonify(ConfigService().add_cities(names or [], int(body.get("ratio") or 0)))


@bp.put("/api/cities/<int:city_id>", endpoint="city_update_api")
def city_update_api(city_id: int):
    return jsonify(ConfigService().update_city(city_id, **_payload()))


@bp.delete("/api/cities/<int:city_id>", endpoint="city_delete_api")
def city_delete_api(city_id: int):
    ConfigService().delete_city(city_id)
    return jsonify({"ok": True})


# ── 读者画像 ────────────────────────────────────────────────────────────────

@bp.get("/api/audience", endpoint="audience_api")
def audience_api():
    """双形状：content（旧契约）+ profile/insight（原站画像页表单）。"""
    return jsonify(ConfigService().get_audience_profile())


@bp.post("/api/audience", endpoint="audience_save_api")
def audience_save_api():
    body = _payload()
    if isinstance(body.get("profile"), dict):
        # 页面回传当前洞察（保存画像时一并带回，避免被清掉）
        return jsonify(ConfigService().save_audience_profile(
            body["profile"], body.get("insight")))
    return jsonify(ConfigService().save_audience(body.get("content", "")))


@bp.post("/api/audience/analyze", endpoint="audience_analyze_api")
def audience_analyze_api():
    """AI 洞察：入队 audience.analyze（异步，页面轮询 analyze-status）。

    同一租户并发启动会被拒（can_start），不排队第二个。
    """
    from app.blueprints.config import audience_jobs
    from app.tenancy import current_tenant

    tid = current_tenant()
    # 「保存并 AI 分析」= 先存再分析。以前这里把请求里的 profile **整份丢掉**，
    # 只入队分析任务：用户刚加的痛点/顾虑/场景/地点一个都没落库（返回页面就"消失"），
    # 而且分析任务读的是库里的**旧画像**，洞察也跟着算错。
    body = _payload()
    if isinstance(body.get("profile"), dict):
        ConfigService().save_audience_profile(body["profile"])
    if not audience_jobs.try_begin(tid):    # 原子抢占：入队前先占位，杜绝连点双开
        return jsonify({"ok": False, "error": "洞察分析进行中，请稍候"})
    from app.platform import jobs

    jobs.enqueue("audience.analyze", {}, tenant_id=tid, queue="gen")
    return jsonify({"ok": True})


@bp.get("/api/audience/analyze-status", endpoint="audience_analyze_status_api")
def audience_analyze_status_api():
    from app.blueprints.config import audience_jobs
    from app.tenancy import current_tenant

    return jsonify(audience_jobs.state(current_tenant()))


# ── 素材库（上传/列表/删除真实落盘，Phase III）─────────────────────────────

@bp.get("/api/my-assets", endpoint="my_assets_api")
def my_assets_api():
    return jsonify(AssetsService().list_assets())


@bp.post("/api/my-assets", endpoint="my_assets_upload_api")
def my_assets_upload_api():
    files = request.files.getlist("files") or request.files.getlist("file")
    return jsonify(AssetsService().upload(files))


@bp.post("/api/my-assets/delete", endpoint="my_assets_delete_api")
def my_assets_delete_api():
    return jsonify(AssetsService().delete(_payload().get("id")))


@bp.get("/media/<path:rel_path>", endpoint="media_file_api")
def media_file_api(rel_path: str):
    data = AssetsService().read_bytes(rel_path)
    return Response(data, status=200, mimetype=_mimetype_of(rel_path))


def _mimetype_of(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif"}.get(ext, "application/octet-stream")


# ── AI 指令自定义（原站 /api/my-prompts 契约；保存即生效于下一批生成）────────

@bp.get("/api/my-prompts", endpoint="my_prompts_api")
def my_prompts_api():
    return jsonify(PromptService().list_prompts())


@bp.post("/api/my-prompts/<step>", endpoint="my_prompt_save_api")
def my_prompt_save_api(step: str):
    return jsonify(PromptService().save_prompt(step, _payload().get("content", "")))


@bp.post("/api/my-prompts/<step>/reset", endpoint="my_prompt_reset_api")
def my_prompt_reset_api(step: str):
    return jsonify(PromptService().reset_prompt(step))


# ── 在线模型配置（任意 OpenAI 兼容 LLM / 生图；仅 admin）────────────────────

def _require_admin() -> None:
    from flask import session
    from app.errors import Forbidden
    if session.get("role") != "admin":
        raise Forbidden("模型配置仅运营管理端可修改")


@bp.get("/api/model-config", endpoint="model_config_api")
def model_config_api():
    _require_admin()          # 端点/模型信息只对运营端开放（写接口早已如此）
    return jsonify(ModelSettingsService().get())


@bp.post("/api/model-config", endpoint="model_config_save_api")
def model_config_save_api():
    _require_admin()
    return jsonify(ModelSettingsService().save(_payload()))


@bp.post("/api/model-config/reset", endpoint="model_config_reset_api")
def model_config_reset_api():
    _require_admin()
    return jsonify(ModelSettingsService().reset())


@bp.post("/api/model-config/test-llm", endpoint="model_config_test_llm_api")
def model_config_test_llm_api():
    _require_admin()          # 会用服务器已存的 Key 外呼，客户端不放行
    return jsonify(ModelSettingsService().test_llm(_payload()))


@bp.post("/api/model-config/test-image", endpoint="model_config_test_image_api")
def model_config_test_image_api():
    _require_admin()
    return jsonify(ModelSettingsService().test_image(_payload()))


@bp.post("/api/model-config/test-yxer", endpoint="model_config_test_yxer_api")
def model_config_test_yxer_api():
    _require_admin()
    return jsonify(ModelSettingsService().test_yxer(_payload()))


@bp.post("/api/model-config/test-engine", endpoint="model_config_test_engine_api")
def model_config_test_engine_api():
    """GEO 单引擎连通性实测（admin；不落配置）。"""
    _require_admin()
    return jsonify(ModelSettingsService().test_engine(_payload()))