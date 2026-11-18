"""CSV 快照导出（reports.html / citations.html 的「导出 CSV」按钮）。

三种快照（实时查询当前租户，导出即最新）：
  articles   —— 文章档案（Article 全量）
  publog     —— 发布日志（PublishLog 终态，一个任务一行）
  citations  —— 被引明细（GeoResult 中 cited_lv 且有链接的行，
                同 GEO 看板 cited_ours 口径，见 geo_aggregate）

CSV 契约（前端 `a[download]` 原生锚点）：
- UTF-8 **带 BOM**（Excel 双击直接打开不乱码）
- CRLF 行尾（Excel 友好）
- 公式注入防护：以 = + - @ 制表符开头的单元格加 ' 前缀（Excel 不执行公式）
- 无数据 → 只有表头（空形状，不伪造行）
"""
from __future__ import annotations

import csv
import io

from app.extensions import db as _db
from app.models import Article, GeoResult, PublishLog
from app.services.geo_aggregate import _platform_of, _title_of
from app.tenancy import TenantScopedRepository
from app.timeutil import hm_string

_BOM = "﻿"          # UTF-8 BOM：Excel 双击直接打开不乱码（页面契约原文）

# 公式注入高危首字符（Excel / WPS 会把这类单元格当公式执行）
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def _cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in _FORMULA_LEADS else text


def _dump(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows([[_cell(c) for c in row] for row in rows])
    return _BOM + buf.getvalue()


def _articles() -> tuple[str, list[str], list[list]]:
    rows = (TenantScopedRepository(Article).query()
            .order_by(Article.id.asc()).all())
    return ("articles", ["目录", "标题", "状态", "初稿时间", "审稿时间", "定稿时间"],
            [[a.dir, a.title or "", a.status, _hm(a.draft_at), _hm(a.review_at),
              _hm(a.final_at)] for a in rows])


def _publog() -> tuple[str, list[str], list[list]]:
    rows = (TenantScopedRepository(PublishLog).query()
            .order_by(PublishLog.id.desc()).all())
    return ("publog", ["时间", "文章标题", "账号", "账号ID", "平台", "模式",
                       "成功", "错误", "链接"],
            [[_hm(r.happened_at), r.title or r.article_dir, r.account_name or "",
              r.account_id or "", r.platform or "", r.mode or "",
              "是" if r.success else "否", r.error or "", r.url or ""] for r in rows])


def _citations() -> tuple[str, list[str], list[list]]:
    grepo = TenantScopedRepository(GeoResult)
    cited = [r for r in grepo.query().all() if r.cited_lv and r.cited_url]
    cited.sort(key=lambda r: r.sampled_at, reverse=True)
    dir_title = {a.dir: a.title or a.dir
                 for a in TenantScopedRepository(Article).query().all()}
    return ("citations", ["发现时间", "引擎", "平台", "提问词", "文章标题", "被引链接"],
            [[_hm(r.sampled_at), r.engine, _platform_of(r.cited_url), r.query,
              _title_of(r.cited_url, dir_title), r.cited_url] for r in cited])


def _hm(dt) -> str:
    return hm_string(dt) if dt is not None else ""


_BUILDERS = {"articles": _articles, "publog": _publog, "citations": _citations}


def build_csv(kind: str) -> tuple[str, str]:
    """→ (下载文件名, CSV 文本含 BOM)。未知 kind → ValueError（路由层转 400）。"""
    builder = _BUILDERS.get(str(kind or ""))
    if builder is None:
        raise ValueError(f"不支持的导出类型：{kind}（可选 {sorted(_BUILDERS)}）")
    name, header, rows = builder()
    return f"geo-{name}-{_stamp()}.csv", _dump(header, rows)


def _stamp() -> str:
    from app.timeutil import now_local
    return now_local().strftime("%Y%m%d-%H%M")
