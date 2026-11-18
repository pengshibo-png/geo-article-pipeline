"""CSV 导出（reports.html / citations.html 的「导出 CSV」按钮，2026-10-14 补）。

契约（前端 `a[download]` 原生锚点，不过 JS）：
- GET /api/reports/export?type=articles|publog|citations → text/csv 附件
- 带 UTF-8 BOM（Excel 双击直接打开不乱码）
- 数据实时生成，导出即最新快照；未知 type → 400
- 公式注入防护：以 = + - @ 开头的单元格加 ' 前缀（Excel 不执行公式）
"""
from __future__ import annotations

import csv
import io

from app.models import Article, GeoResult, PublishLog
from app.tenancy import tenant_scope
from app.timeutil import now_local
from tests.conftest import T1, T2, login


def _rows(resp) -> list[list[str]]:
    text = resp.get_data(as_text=True)
    assert text.startswith("﻿"), "CSV 必须带 UTF-8 BOM（Excel 直接打开）"
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def _seed(app):
    """一个租户的种子：2 篇文章 + 1 条发布日志 + 2 条监测样本（1 条被引）。"""
    with app.app_context():
        with tenant_scope(T1):
            from app.extensions import db

            db.session.add_all([
                Article(dir="文章甲", title="文章甲", status="final",
                        draft_at=now_local(), review_at=now_local(), final_at=now_local()),
                Article(dir="文章乙", title="文章乙", status="draft",
                        draft_at=now_local()),
                PublishLog(task_id=1, article_dir="文章甲", title="文章甲",
                           account_id="ac-1", account_name="搜狐号A", platform="souhu",
                           mode="real", success=True, url="https://sohu.example/a",
                           happened_at=now_local()),
                GeoResult(query="杭州断镯修复哪家好", engine="doubao",
                          channel="api", mentioned=True, rank=2, cited_lv=True,
                          cited_url="https://sohu.example/a", sampled_at=now_local()),
                GeoResult(query="杭州断镯修复哪家好", engine="qwen",
                          channel="api", mentioned=False, sampled_at=now_local()),
            ])
            db.session.commit()


def test_export_articles_csv(client, app):
    login(client, "alpha", "pw-alpha")
    _seed(app)
    r = client.get("/api/reports/export?type=articles")
    assert r.status_code == 200 and "text/csv" in r.content_type
    rows = _rows(r)
    assert rows[0] == ["目录", "标题", "状态", "初稿时间", "审稿时间", "定稿时间"]
    assert len(rows) == 3                     # 表头 + 2 篇
    assert rows[1][1] == "文章甲" and rows[1][2] == "final"
    assert rows[2][2] == "draft" and rows[2][4] == ""   # 未审稿 → 空单元格


def test_export_publog_csv(client, app):
    login(client, "alpha", "pw-alpha")
    _seed(app)
    rows = _rows(client.get("/api/reports/export?type=publog"))
    assert rows[0][0] == "时间" and "文章标题" in rows[0]
    assert len(rows) == 2
    assert rows[1][1] == "文章甲" and rows[1][4] == "souhu" and "sohu.example" in rows[1][-1]


def test_export_citations_only_cited_rows(client, app):
    """被引明细：只导 cited_lv=True 且有链接的行（同看板 cited_ours 口径）。"""
    login(client, "alpha", "pw-alpha")
    _seed(app)
    rows = _rows(client.get("/api/reports/export?type=citations"))
    assert rows[0] == ["发现时间", "引擎", "平台", "提问词", "文章标题", "被引链接"]
    assert len(rows) == 2                     # 2 条样本只有 1 条被引
    assert rows[1][1] == "doubao" and "sohu.example" in rows[1][5]


def test_export_unknown_type_rejected(client):
    login(client, "alpha", "pw-alpha")
    r = client.get("/api/reports/export?type=nope")
    assert r.status_code == 400 and "不支持" in r.get_json()["error"]


def test_export_requires_login(client):
    assert client.get("/api/reports/export?type=articles").status_code == 401


def test_export_escapes_formula_injection(client, app):
    """以 = 开头的标题 → 单元格加 ' 前缀（Excel 公式注入防护）。"""
    with app.app_context():
        with tenant_scope(T1):
            from app.extensions import db

            db.session.add(Article(dir="=cmd|'/c calc'!A1", title="=1+1",
                                   status="draft", draft_at=now_local()))
            db.session.commit()
    login(client, "alpha", "pw-alpha")
    rows = _rows(client.get("/api/reports/export?type=articles"))
    assert rows[1][0] == "'=cmd|'/c calc'!A1" and rows[1][1] == "'=1+1"


def test_export_empty_tenant_header_only(client, app):
    """无数据 → 只有表头行（空形状，不伪造行）。"""
    login(client, "beta", "pw-beta")
    rows = _rows(client.get("/api/reports/export?type=articles"))
    assert len(rows) == 1 and rows[0][0] == "目录"
