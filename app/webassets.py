"""静态资产版本号：模板里统一 `?v={{ asset_ver }}`。

资产/模板变更时升位常量即可让浏览器缓存失效（双引擎缓存风险的对策）。
"""
from __future__ import annotations

from flask import Flask

ASSET_VER = "20261012a"


def install_asset_version(app: Flask) -> None:
    @app.context_processor
    def _inject_asset_ver():                    # pragma: no cover - 简单透传
        return {"asset_ver": ASSET_VER}
