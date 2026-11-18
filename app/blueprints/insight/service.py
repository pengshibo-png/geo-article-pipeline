"""insight 蓝图 · 领域服务。

GEO 看板聚合逻辑下沉在 `app/services/geo_monitor.py`（口径唯一出口，
报告生成也复用它）；本层只做仓储读取 + 形状转发。
"""
from __future__ import annotations

from app.extensions import db as _db
from app.services import geo_monitor


class InsightService:
    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    def dashboard(self) -> dict:
        return geo_monitor.dashboard(session=self.session)
