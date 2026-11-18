"""读者画像 AI 洞察存储（落租户目录，与 douyin/video 的 *_store 同构）。

洞察是**可再生成**的派生数据，故不进库（避免为它单独加列/迁移），
与内容包、成片一样按租户落盘。
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import current_app

from app.tenancy import current_tenant

_INSIGHT_FILE = "insight.json"


def root() -> Path:
    return Path(current_app.config["DATA_ROOT"]) / current_tenant() / "audience"


def load_insight() -> str:
    p = root() / _INSIGHT_FILE
    if not p.exists():
        return ""
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("insight") or "")
    except (json.JSONDecodeError, OSError):
        return ""


def save_insight(text: str) -> None:
    p = root() / _INSIGHT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"insight": str(text or "")}, ensure_ascii=False),
                 encoding="utf-8")


__all__ = ["load_insight", "save_insight"]
