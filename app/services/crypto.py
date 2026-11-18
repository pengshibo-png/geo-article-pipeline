"""静态加密（自媒体账号 Cookie 等；models/account.py 规约"加密存储，禁明文"）。

Fernet 密钥由应用 SECRET_KEY 派生（sha256 → urlsafe64），跨重启稳定：
桌面版 SECRET_KEY 固定存于 data/secret.txt（bootstrap），解密始终可恢复。

cryptography 未安装时 encrypt/decrypt 抛 ConfigError —— 调用端点如实报错，
**绝不退回明文存储**。
"""
from __future__ import annotations

import base64
import hashlib

from flask import current_app

from app.errors import ConfigError


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise ConfigError("缺少 cryptography 依赖，无法加密存储 Cookie（请安装 requirements 后重试）") from exc
    secret = str(current_app.config["SECRET_KEY"])
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_text(text: str) -> bytes:
    return _fernet().encrypt((text or "").encode("utf-8"))


def decrypt_text(blob: bytes | None) -> str:
    if not blob:
        return ""
    try:
        return _fernet().decrypt(bytes(blob)).decode("utf-8")
    except Exception as exc:  # InvalidToken = SECRET_KEY 变更/数据损坏
        raise ConfigError("Cookie 解密失败（密钥已变更？）：请重新提交 Cookie") from exc
