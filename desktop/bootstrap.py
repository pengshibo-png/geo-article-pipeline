"""首启引导：建目录 / 建表 / 种本地租户与账号（幂等，可重复执行）。

种子模式沿用 tests/conftest.py 的受控例外：**无租户上下文时显式传 tenant_id**
（before_flush 兜底不介入的身份域写路径）。
"""
from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path

from flask import Flask

from app.logging import get_logger

log = get_logger(__name__)

LOCAL_TENANT = "local"
LOCAL_USER = "local"


def ensure_first_run(app: Flask) -> bool:
    """空库才种子；已初始化（租户已存在）则只补目录。返回是否做了种子。"""
    from app.extensions import db

    data_root = Path(app.config["DATA_ROOT"])
    (data_root / "logs").mkdir(parents=True, exist_ok=True)

    with app.app_context():
        from app.models import AppUser, Brand, Tenant, TitleConfig

        db.create_all()
        if db.session.get(Tenant, LOCAL_TENANT) is not None:
            db.session.rollback()
            return False

        password = secrets.token_urlsafe(12)
        db.session.add_all([
            Tenant(id=LOCAL_TENANT, name="本机工作台", status="active"),
            AppUser(tenant_id=LOCAL_TENANT, username=LOCAL_USER,
                    password_hash=_hash(password), role="admin", active=True),
            Brand(tenant_id=LOCAL_TENANT, content=""),
            TitleConfig(tenant_id=LOCAL_TENANT, timeliness_ratio=25),
        ])
        # 会话随 app 上下文回收时回滚未提交写入，跨块读取前必须显式提交
        # （与 platform/jobs.run_job 的会话边界同一约定）。
        db.session.commit()
        log.info("desktop_first_run", extra={"event": "desktop_first_run"})
    _write_credentials(data_root, password)
    return True


def _hash(raw: str) -> str:
    from app.security import hash_password

    return hash_password(raw)          # 首启一次性开销，用默认算法即可


def _write_credentials(data_root: Path, password: str) -> None:
    """随机口令落盘作保险：自动登录下通常用不到，忘记口令时可查。"""
    cred = data_root / "credentials.txt"
    if cred.exists():
        return
    cred.write_text(
        "GEO 工作台本地账号（自动登录，一般无需手工使用）\n"
        f"创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"用户名: {LOCAL_USER}\n"
        f"密码: {password}\n",
        encoding="utf-8",
    )
