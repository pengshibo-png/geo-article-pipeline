"""topic 蓝图 · JSON 接口（同 views 的职责，返回 JSON）。"""
from __future__ import annotations

from flask import jsonify, request

from app.blueprints.topic import bp
from app.blueprints.topic.service import TopicService
from app.blueprints.topic.titles_service import AutoschedService, TitlesService
from app.platform import jobs
from app.services.writer import managed
from app.tenancy import current_tenant


def _int_or(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


@bp.get("/api/topic/ping", endpoint="topic_ping")
def topic_ping():
    return jsonify({"ok": True, "blueprint": "topic"})


# ── 选题配置（原站「AI 指令自定义」页右下卡 + 选题驱动）──────────────────────

@bp.get("/api/my-title-config", endpoint="my_title_config_api")
def my_title_config_api():
    return jsonify(TopicService().get_title_config())


@bp.post("/api/my-title-config", endpoint="my_title_config_save_api")
def my_title_config_save_api():
    body = request.get_json(silent=True) or {}
    return jsonify(TopicService().save_title_config(body))


# ── 选题库（titles.html）─────────────────────────────────────────────────────

@bp.get("/api/titles", endpoint="titles_api")
def titles_api():
    return jsonify(TitlesService().list_titles())


@bp.post("/api/titles", endpoint="titles_save_api")
def titles_save_api():
    body = request.get_json(silent=True) or {}
    svc = TitlesService()
    if str(body.get("mode") or "append") == "single_delete":
        return jsonify(svc.delete_title(body.get("title")))
    return jsonify(svc.add_titles(body.get("titles") or "",
                                  source=str(body.get("source") or "manual")))


@bp.post("/api/titles/generate", endpoint="titles_generate_api")
def titles_generate_api():
    body = request.get_json(silent=True) or {}
    tid = current_tenant()
    if managed.titles_running(tid):
        return jsonify({"ok": False, "error": "已有选题生成任务在运行，请等待完成"})
    count = max(1, min(_int_or(body.get("count"), 20), 200))
    target = max(count, min(_int_or(body.get("target"), 0) or count, 1000))
    managed.titles_begin(tid)          # 先置 running，再入队（轮询不读到起跑前的假空闲）
    jobs.enqueue("titles.generate.managed", {"count": count, "target": target},
                 tenant_id=tid, queue="gen")
    return jsonify({"ok": True, "started": True, "running": True})


@bp.get("/api/titles/generate-status", endpoint="titles_generate_status_api")
def titles_generate_status_api():
    return jsonify(managed.titles_snapshot(current_tenant()))


@bp.post("/api/titles/generate-stop", endpoint="titles_generate_stop_api")
def titles_generate_stop_api():
    managed.titles_request_stop(current_tenant())
    return jsonify({"ok": True})


# ── 同行挖掘（LLM 推演同行标题 → CompetitorTitle 学习库）────────────────────

@bp.post("/api/titles/scrape-competitors", endpoint="titles_scrape_api")
def titles_scrape_api():
    """入队 titles.scrape（异步，页面轮询 scrape-status）。

    诚实口径：本系统不爬取外部站点，标题由 LLM 按本租户业务/城市推演，
    作为"AI 智能选题"的训练材料（与页面文案一致）。
    """
    from app.blueprints.topic import scrape_jobs
    from app.tenancy import current_tenant

    tid = current_tenant()
    if not scrape_jobs.try_begin(tid):      # 原子抢占：入队前先占位，杜绝连点双开
        return jsonify({"ok": False, "error": "挖掘进行中，请稍候"})
    from app.platform import jobs

    jobs.enqueue("titles.scrape", {"count": 20}, tenant_id=tid, queue="gen")
    return jsonify({"ok": True, "started": True})


@bp.get("/api/titles/scrape-status", endpoint="titles_scrape_status_api")
def titles_scrape_status_api():
    from app.blueprints.topic import scrape_jobs
    from app.tenancy import current_tenant

    s = scrape_jobs.state(current_tenant())
    s["library"] = TitlesService().list_titles()["library"]
    return jsonify(s)


# ── 生成计划（titles.html / gen.html 共用；落库 Schedule 表，执行器 Phase III）──

@bp.get("/api/autosched", endpoint="autosched_api")
def autosched_api():
    return jsonify(AutoschedService().get())


@bp.post("/api/autosched", endpoint="autosched_save_api")
def autosched_save_api():
    body = request.get_json(silent=True) or {}
    return jsonify(AutoschedService().save(body))
