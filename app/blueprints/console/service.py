"""console 蓝图 · 领域服务（业务规则/编排；不 import Flask request，不写裸 SQL）。

Phase 0 为骨架，Phase 2+ 逐个填充。所有数据访问必须经 TenantScopedRepository。
"""
from __future__ import annotations

from app.logging import get_logger

log = get_logger(__name__)


class ConsoleService:
    """首页聚合 4 个域的只读摘要"""

    def __init__(self, session=None) -> None:
        self.session = session
