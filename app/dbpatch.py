"""存量 SQLite 库的运行时轻量补列（桌面 exe / 本地开发路径）。

背景：Flask-SQLAlchemy 的 `create_all()` 只建缺的**表**，不会给已存在的**表**加列。
桌面 exe 升级时数据目录里的旧 app.db 就属于这种情况——走 alembic 对用户太重，
故在 create_app 末尾做幂等补列：PRAGMA 查列、缺则 ALTER TABLE ADD COLUMN。

PostgreSQL（SaaS V5 路径）**不走这里**，由 alembic 迁移负责（ADR-004）；
alembic 迁移 a7f2c4e19b03 与本文件的 DDL 保持一致。

每个补丁项 = (表, 列, 列 DDL)。只在 sqlite 方言且表已存在时执行；
新库由 create_all 一次建成全列，PRAGMA 命中后自然跳过。
"""
from __future__ import annotations

from flask import Flask
from sqlalchemy import text

from app.extensions import db
from app.logging import get_logger

log = get_logger(__name__)

# (表名, 列名, "列名 类型 约束")——与 models 里的定义逐字对应
_PATCHES: tuple[tuple[str, str, str], ...] = (
    ("title_config", "city_ratio", "city_ratio INTEGER NOT NULL DEFAULT 60"),
    ("schedule", "config", "config TEXT"),
    # report 按施工规格冻结 DDL 补列（models/report.py docstring 有交代）
    ("report", "status", "status VARCHAR(16) NOT NULL DEFAULT 'queued'"),
    ("report", "step", "step VARCHAR(16)"),
    ("report", "tokens_in", "tokens_in INTEGER NOT NULL DEFAULT 0"),
    ("report", "tokens_out", "tokens_out INTEGER NOT NULL DEFAULT 0"),
    ("report", "cost", "cost DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ("report", "summary_line", "summary_line TEXT"),
    ("report", "share_token", "share_token VARCHAR(64)"),
    ("report", "share_active", "share_active BOOLEAN NOT NULL DEFAULT 0"),
    ("report", "share_expires", "share_expires DATETIME"),
    ("account", "yxer_id", "yxer_id VARCHAR(64)"),
    # 发布结果回查（蚁小二异步发布，提交 ≠ 已发布）
    ("publish_log", "remote_status", "remote_status VARCHAR(16)"),
    ("publish_log", "remote_detail", "remote_detail TEXT"),
    ("publish_log", "remote_url", "remote_url VARCHAR(512)"),
    ("publish_log", "checked_at", "checked_at DATETIME"),
)


def patch_sqlite_columns(app: Flask) -> None:
    """幂等补列。任何失败都不阻断启动（打 warning 后继续）——
    补列失败只影响「业务城市页的城市词占比」这一项功能，不值得拖垮整个应用。"""
    try:
        with app.app_context():
            if db.engine.dialect.name != "sqlite":
                return
            with db.engine.begin() as conn:
                for table, column, ddl in _PATCHES:
                    if not _table_exists(conn, table):
                        continue
                    if _has_column(conn, table, column):
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
                    log.warning("已为存量库补列 %s.%s", table, column)
                _rebuild_geo_result(conn)
    except Exception:  # noqa: BLE001 - 见 docstring：失败不阻断启动
        log.warning("存量库补列检查失败（忽略）", exc_info=True)


def _rebuild_geo_result(conn) -> None:
    """geo_result 的 channel 约束加值（加入 'simulate'）——SQLite 改不了 CHECK，
    只能重建表：读旧行 → DROP → 按新模型建表 → 回填。监测结果表很小，内存拷贝可接受。
    幂等：DDL 已含 'simulate' 时直接跳过。"""
    sql = conn.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='geo_result'")).scalar()
    if not sql or "simulate" in sql:
        return
    from app.models import GeoResult

    cols = [c.name for c in GeoResult.__table__.columns]
    rows = [dict(r._mapping) for r in conn.execute(text("SELECT * FROM geo_result"))]
    conn.execute(text("DROP TABLE geo_result"))          # DROP 会连带删掉旧索引，避免重名冲突
    GeoResult.__table__.create(bind=conn)
    if rows:
        col_list = ", ".join(cols)
        placeholders = ", ".join(f":{c}" for c in cols)
        conn.execute(text(f"INSERT INTO geo_result ({col_list}) VALUES ({placeholders})"),
                     [{k: r.get(k) for k in cols} for r in rows])
    log.warning("已重建 geo_result（channel 约束加入 simulate），回填 %d 行", len(rows))


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).first()
    return row is not None


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    return any(r[1] == column for r in rows)
