"""诊断报告生成：数据采集 → LLM 结论 → 校验 → HTML 落盘（report.* 任务）。

流水线（tasks.html DIAG_STEP_CN 状态机的服务端实现）：
queued → generating(采集+结论) → verifying(数字对账) → rendering(HTML) → done|failed

- 失败原因写 Report.note（页面 status 的 fail_reason）；失败可从原 step 重试。
- HTML 落在租户数据目录 reports/<id>.html；"PDF" 本期返回同一 HTML 的打印版
  （window.print 触发浏览器另存），真实 PDF 渲染需 weasyprint 类系统依赖，后续接。
- 数据不足（无文章/无发布/无监测）与超 14 天未更新 → 生成请求直接被拒（页面
  原样展示中文 error 文案）。
"""
from __future__ import annotations

import hashlib
import html as _html
from datetime import datetime

from flask import current_app

from app.logging import get_logger
from app.platform import storage
from app.platform.jobs import task
from app.services.llm import build_llm_client
from app.tenancy import TenantScopedRepository, current_tenant
from app.timeutil import hm_string, now_local

log = get_logger(__name__)

_STALE_DAYS = 14
_UNIT_COST = 0.02          # 每份报告名义成本（元）；真实计费接入后替换


def esc(text) -> str:
    return _html.escape(str(text if text is not None else ""))


def collect_data(session=None) -> dict:
    """报告数据源汇总（发布日志 + 文章档案 + 窗口）。GEO 看板由调用方另取。"""
    return _collect_via(session)


def _collect_via(session) -> dict:
    from datetime import timedelta

    from app.models import Article, PublishLog

    now = now_local()
    window_from = now.date() - timedelta(days=13)
    week_ago = now - timedelta(days=7)
    arts = TenantScopedRepository(Article, session).query().order_by(
        Article.created_at.desc()).all()
    logs = TenantScopedRepository(PublishLog, session).query().all()

    def _aware(dt):
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)

    pub_ok_logs = [x for x in logs if x.success]
    return {
        "generated_at": hm_string(now),
        "window_from": window_from.strftime("%Y-%m-%d"),
        "window_to": now.strftime("%Y-%m-%d"),
        "articles_total": len(arts),
        "articles_recent": [{"dir": a.dir, "title": a.title,
                             "created": hm_string(a.created_at),
                             "status": a.status} for a in arts[:10]],
        "pub_total": len(logs),
        "pub_ok": len(pub_ok_logs),
        "pub_week": sum(1 for x in pub_ok_logs
                        if _aware(x.happened_at) and _aware(x.happened_at) >= week_ago),
        "pub_urls": [x.url for x in pub_ok_logs if x.url][:10],
    }


def data_gate(data: dict, geo_overall: dict) -> str | None:
    """生成前置检查；返回中文拒绝理由或 None（放行）。"""
    if not data["articles_total"] and not data["pub_total"] and not geo_overall:
        return "还没有可用于报告的数据：请先在「文章生成」产出内容，或先跑一轮 GEO 监测"
    return None


def build_html(data: dict, geo: dict, summary: str, tenant_name: str, report_id: str) -> str:
    o = geo.get("overall") or {}
    trend_rows = "".join(
        f"<tr><td>{esc(t['date'])}</td><td>{esc(t.get('mention_rate', 0))}%</td>"
        f"<td>{esc(t.get('cite_rate', 0))}%</td></tr>" for t in (geo.get("trend") or [])[-14:])
    art_rows = "".join(
        f"<tr><td>{esc(a['title'])}</td><td>{esc(a['created'])}</td>"
        f"<td>{esc(a['status'])}</td></tr>" for a in data["articles_recent"])
    cite_rows = "".join(
        f"<tr><td>{esc(c['engine'])}</td><td><a href=\"{esc(c['url'])}\">{esc(c['title'])}</a></td>"
        f"<td>{esc(c['time'])}</td></tr>" for c in (geo.get("cited_ours") or [])[:10])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{esc(tenant_name)} · 诊断报告 {esc(report_id)}</title>
<style>
body{{font:14px/1.8 "Microsoft YaHei",sans-serif;color:#1c2430;max-width:860px;margin:24px auto;padding:0 16px}}
h1{{font-size:22px;border-bottom:3px solid #B08D57;padding-bottom:8px}}
h2{{font-size:16px;margin-top:28px;color:#8a6c3f}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border:1px solid #d9dee6;padding:6px 10px;text-align:left}}
th{{background:#f4f1ea}}
.kpi{{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0}}
.kpi b{{font-size:22px;display:block}}
.kpi span{{border:1px solid #d9dee6;border-radius:6px;padding:8px 16px;min-width:110px}}
.sum{{background:#faf7f1;border-left:4px solid #B08D57;padding:12px 16px;border-radius:4px}}
@media print{{.noprint{{display:none}}}}
</style></head><body>
<h1>{esc(tenant_name)} · GEO 诊断报告</h1>
<p>统计窗口 {esc(data['window_from'])} ～ {esc(data['window_to'])} · 生成时间 {esc(data['generated_at'])} · 报告号 {esc(report_id)}</p>
<div class="kpi">
  <span><b>{data['articles_total']}</b>内容产出（篇）</span>
  <span><b>{data['pub_ok']}/{data['pub_total']}</b>发布成功</span>
  <span><b>{esc(o.get('valid', 0))}</b>监测问答（次）</span>
  <span><b>{esc(o.get('mention_rate', 0))}%</b>品牌提及率</span>
  <span><b>{esc(o.get('cite_rate', 0))}%</b>内容被引率</span>
</div>
<h2>AI 结论</h2><div class="sum">{esc(summary)}</div>
<h2>引擎表现（统计当日）</h2>
<table><tr><th>引擎</th><th>问答数</th><th>提及率</th><th>首位率</th><th>前三率</th><th>被引率</th></tr>
{''.join(f"<tr><td>{esc(k)}</td><td>{esc(v.get('valid', 0))}</td><td>{esc(v.get('mention_rate', 0))}%</td><td>{esc(v.get('top_rate', 0))}%</td><td>{esc(v.get('top3_rate', 0))}%</td><td>{esc(v.get('cite_rate', 0))}%</td></tr>" for k, v in (geo.get('per_engine') or {}).items())}
</table>
<h2>提及率趋势</h2>
<table><tr><th>日期</th><th>提及率</th><th>被引率</th></tr>{trend_rows or '<tr><td colspan=3>窗口内暂无监测数据</td></tr>'}</table>
<h2>最近内容产出</h2>
<table><tr><th>标题</th><th>创建</th><th>状态</th></tr>{art_rows or '<tr><td colspan=3>暂无文章</td></tr>'}</table>
<h2>AI 引用去向</h2>
<table><tr><th>引擎</th><th>被引内容</th><th>时间</th></tr>{cite_rows or '<tr><td colspan=3>暂未被引用</td></tr>'}</table>
<p class="noprint" style="margin-top:32px;color:#8a93a3">本页为打印版报告：浏览器菜单「打印 → 另存为 PDF」即可存档。</p>
<script>window.addEventListener('load',()=>setTimeout(()=>{{if(matchMedia('print').matches===false&&location.hash==='print')window.print()}},300))</script>
</body></html>"""


def verify_numbers(data: dict, geo: dict) -> str | None:
    """数字对账（verifying 步骤）：报告引用的聚合值必须能由数据源复算得到。"""
    o = geo.get("overall") or {}
    if o:
        per = (geo.get("per_engine") or {}).values()
        valid = sum(int(p.get("valid", 0)) for p in per)
        if valid != int(o.get("valid", -1)):
            return "看板聚合值与各引擎明细不一致（valid 对账失败）"
    if data["pub_ok"] > data["pub_total"]:
        return "发布成功数大于发布总数"
    return None


def new_report_id(tenant: str, now: datetime) -> str:
    digest = hashlib.sha1(f"{tenant}{now.timestamp()}".encode()).hexdigest()[:4]
    return f"r_{now.strftime('%Y%m%d_%H%M')}_{tenant}_{digest}"


def write_report_html(report_id: str, content: str) -> str:
    """报告 HTML 落租户数据目录 reports/，返回相对路径（storage 越界防护同文章）。"""
    target = storage.tenant_path("reports", report_id + ".html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"reports/{report_id}.html"


def read_report_html(html_path: str | None):
    if not html_path:
        return None
    path = storage.tenant_path(*html_path.split("/"))
    return path.read_text(encoding="utf-8") if path.exists() else None


# 一条 UPDATE 完成「跨月重置 + 额度校验 + 自增 + 累计成本」：
# **不要先 SELECT 再 UPDATE**（与 Account.quota_update_sql 同套路）——
# 并发生成报告时会超额出报告。放在 services 层：blueprints 下不许裸 session.execute（V1）。
_CONSUME_QUOTA_SQL = (
    "UPDATE report_quota SET "
    "  period_start = :first, "
    "  used = CASE WHEN period_start = :first THEN used + 1 ELSE 1 END, "
    "  total_cost = total_cost + :cost "
    "WHERE tenant_id = :tid AND ("
    "  period_start IS NULL OR period_start <> :first OR used < quota_limit)"
)


def consume_quota(session, tid: str, *, first) -> bool:
    """原子扣减 1 份报告额度。False = 额度不足（并发下也不会超额）。"""
    from sqlalchemy import text

    res = session.execute(text(_CONSUME_QUOTA_SQL),
                          {"first": first, "cost": _UNIT_COST, "tid": tid})
    return int(res.rowcount or 0) > 0


def _summary_prompt(data: dict, geo: dict, tenant_name: str) -> str:
    o = geo.get("overall") or {}
    return (f"你是GEO优化顾问。基于以下事实给客户「{tenant_name}」写一段 120 字以内的"
            f"诊断结论（直接给判断与下一步动作，不堆数据）：内容产出 {data['articles_total']} 篇；"
            f"发布成功 {data['pub_ok']}/{data['pub_total']}；AI 监测问答 {o.get('valid', 0)} 次，"
            f"提及率 {o.get('mention_rate', 0)}%，首位率 {o.get('top_rate', 0)}%，"
            f"被引率 {o.get('cite_rate', 0)}%。")


@task("report.generate")
def report_generate_job(payload: dict) -> dict:
    """对已入队的 Report 行执行三段流水线（状态与产物全部落库/落盘）。"""
    from app.extensions import db
    from app.models import Report

    rid = str(payload.get("report_id") or "")
    row = TenantScopedRepository(Report).query().filter(Report.id == rid).first()
    if row is None:
        raise ValueError(f"报告不存在: {rid}")

    def _step(name: str) -> None:
        row.status = name if name in ("queued", "done", "failed") else "generating"
        row.step = name
        db.session.commit()

    try:
        from app.services import geo_monitor

        _step("generating")
        data = _collect_via(db.session)
        geo = geo_monitor.dashboard(session=db.session)
        llm = build_llm_client(current_app.config["GEO_SETTINGS"])
        prompt = _summary_prompt(data, geo, row.title or "")
        summary = llm.complete([{"role": "user", "content": prompt}])
        row.tokens_in = len(prompt) // 2
        row.tokens_out = len(summary) // 2

        _step("verifying")
        bad = verify_numbers(data, geo)
        if bad:
            raise RuntimeError(bad)

        _step("rendering")
        html = build_html(data, geo, summary, row.title or "", row.id)
        row.html_path = write_report_html(row.id, html)
        row.summary_line = summary.strip()[:500]
        row.cost = _UNIT_COST
        _step("done")
        log.info("report_done", extra={"event": "report_done", "report_id": rid})
        return {"ok": True, "report_id": rid}
    except Exception as exc:
        row.status = "failed"
        row.note = str(exc)[:500]
        db.session.commit()
        log.warning("report_failed", extra={"event": "report_failed", "report_id": rid})
        return {"ok": False, "report_id": rid, "error": str(exc)}
