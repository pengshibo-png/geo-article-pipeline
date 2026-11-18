"""Flask 扩展单例集中处。

只放"与 Flask app 绑定的扩展对象"，不放业务 Provider（那些放 services/）。
"""
from __future__ import annotations

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
