"""insight 蓝图 · JSON 接口。

GEO 监测（geo.html / citations.html / root.html 契约）：
- GET  /api/geo/dashboard → 真实聚合（无数据显示空形状，不伪造）
- POST /api/geo/run {engines,rounds,limit} → 入队采样（页面不等响应，轮询 status）
- GET  /api/geo/status → {running, log_tail}
- POST /api/geo/stop → 本批后收束
"""
from __future__ import annotations

from flask import jsonify, request, Response

from app.blueprints.insight import bp
from app.blueprints.insight.report_service import ReportService
from app.blueprints.insight.service import InsightService
from app.platform import jobs
from app.services import geo_monitor
from app.tenancy import current_tenant


@bp.get("/api/insight/ping", endpoint="insight_ping")
def insight_ping():
    return jsonify({"ok": True, "blueprint": "insight"})


@bp.get("/api/geo/dashboard", endpoint="geo_dashboard_api")
def geo_dashboard_api():
    return jsonify(InsightService().dashboard())


@bp.post("/api/geo/run", endpoint="geo_run_api")
def geo_run_api():
    tid = current_tenant()
    if geo_monitor.is_running(tid):
        return jsonify({"ok": False, "error": "已有监测任务在运行"})
    body = request.get_json(silent=True) or {}
    geo_monitor.begin(tid, 0)
    jobs.enqueue("geo.run", {"engines": body.get("engines"), "rounds": body.get("rounds", 1),
                             "limit": body.get("limit", 0)}, tenant_id=tid, queue="geo")
    return jsonify({"ok": True, "started": True, "running": True})


@bp.get("/api/geo/status", endpoint="geo_status_api")
def geo_status_api():
    return jsonify(geo_monitor.snapshot(current_tenant()))


@bp.post("/api/geo/stop", endpoint="geo_stop_api")
def geo_stop_api():
    geo_monitor.request_stop(current_tenant())
    return jsonify({"ok": True})


# ── 诊断报告（reports.html）─────────────────────────────────────────────────

@bp.get("/api/report/list", endpoint="report_list_api")
def report_list_api():
    a = request.args
    return jsonify(ReportService().list_reports(
        page=a.get("page", 1), size=a.get("size", 20), status=a.get("status", ""),
        days=a.get("days", 0), q=a.get("q", "")))


@bp.post("/api/report/generate", endpoint="report_generate_api")
def report_generate_api():
    body, code = ReportService().generate()
    return jsonify(body), code


@bp.get("/api/report/status", endpoint="report_status_api")
def report_status_api():
    body, code = ReportService().status(request.args.get("id", ""))
    return jsonify(body), code


@bp.get("/api/report/html", endpoint="report_html_api")
def report_html_api():
    content = ReportService().html(request.args.get("id", ""))
    return Response(content, mimetype="text/html")


@bp.get("/api/report/pdf", endpoint="report_pdf_api")
def report_pdf_api():
    """打印版报告：HTML + attachment，浏览器打开即弹「打印/另存 PDF」。

    真 PDF 渲染（weasyprint）需系统级依赖，桌面精简包不带——后续接。
    """
    rid = request.args.get("id", "")
    content = ReportService().html(rid)
    return Response(content, mimetype="text/html",
                    headers={"Content-Disposition": f'attachment; filename="{rid}.html"'})


@bp.post("/api/report/retry", endpoint="report_retry_api")
def report_retry_api():
    body = request.get_json(silent=True) or {}
    out, code = ReportService().retry(str(body.get("id") or ""))
    return jsonify(out), code


@bp.post("/api/report/share", endpoint="report_share_api")
def report_share_api():
    body = request.get_json(silent=True) or {}
    out, code = ReportService().share(str(body.get("id") or ""), int(body.get("days") or 30))
    return jsonify(out), code


@bp.post("/api/report/share/revoke", endpoint="report_revoke_api")
def report_revoke_api():
    body = request.get_json(silent=True) or {}
    out, code = ReportService().revoke_share(str(body.get("token") or ""))
    return jsonify(out), code


@bp.get("/r/<token>", endpoint="report_share_page")
def report_share_page(token):
    """公开分享页（原站语义：匿名外访，token 即授权）。

    PUBLIC_ENDPOINTS 白名单放行本端点；撤回 / 过期 / token 不存在 → 404。
    token 是 16 字节 urlsafe 随机数（secrets.token_urlsafe），不可枚举。
    """
    html = ReportService().shared_html(token)
    if html is None:
        return Response("<h1>分享链接不存在或已过期</h1>", status=404,
                        mimetype="text/html")
    return Response(html, mimetype="text/html")


@bp.get("/api/reports/export", endpoint="reports_export_api")
def reports_export_api():
    """CSV 快照导出（reports/citations 页的「导出 CSV」原生锚点）。

    ?type=articles|publog|citations → text/csv 附件（带 BOM，Excel 直开）；
    实时查询当前租户，导出即最新快照。未知 type → 400。
    """
    from app.services.csv_export import build_csv

    kind = request.args.get("type", "")
    try:
        filename, content = build_csv(kind)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return Response(content, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
