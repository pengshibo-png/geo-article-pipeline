"""配置分层：Base → Dev / Test / Prod。

设计要点（对齐 ARCH-ADR-001 v1.1）：
- ADR-013：时间基准**强制** Asia/Shanghai，配错直接启动失败（漏设会静默错位）。
- 分层靠 GEO_ENV 选择，避免"一套配置走天下"。
- 启动即校验（fail fast）：生产环境 secret_key 不得为默认值。
"""
from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TIMEZONE_REQUIRED = "Asia/Shanghai"          # ADR-013
INSECURE_SECRET = "dev-insecure-change-me"
logger = logging.getLogger(__name__)


class BaseConfig(BaseSettings):
    """所有环境的公共配置。环境变量前缀 GEO_，亦可写 .env。"""

    model_config = SettingsConfigDict(
        env_prefix="GEO_", env_file=".env", extra="ignore", case_sensitive=False
    )

    app_name: str = "geo-article-pipeline"
    env: str = "dev"
    timezone: str = TIMEZONE_REQUIRED        # ADR-013
    secret_key: str = Field(default=INSECURE_SECRET, min_length=8)

    database_url: str = "sqlite:///./data/app.db"
    data_root: Path = PROJECT_ROOT / "data"

    log_level: str = "INFO"
    log_json: bool = True

    # 队列：Phase 0/1 用进程内线程池（ADR-004 阶段一）
    job_backend: str = "threadpool"
    gen_pool_size: int = 4

    # LLM 多 Key 池（AI-ARCH-002）
    llm_provider: str = "fake"               # fake | openai | deepseek | doubao | qwen
    llm_keys: str = ""                       # 逗号分隔；空=未配置
    llm_max_retries: int = 3
    llm_cooldown_seconds: int = 60
    # **任意 OpenAI 兼容端点**（provider=openai 时生效；其余预设内置 URL）
    llm_base_url: str = ""                   # 如 https://api.moonshot.cn/v1
    llm_model: str = ""                      # 如 deepseek-chat / moonshot-v1-8k

    # CSRF 防护：对 POST/PUT/PATCH/DELETE 校验会话令牌（自研，见 app/csrf.py）
    csrf_enabled: bool = True

    # 发布渠道驱动（真实浏览器驱动 = Playwright）
    # auto：账号配了 Cookie 且平台有适配器 → 真发；否则离线模拟（任务日志如实标注）
    # fake：一律离线假驱动（测试/演示）
    publish_driver: str = "auto"
    publish_headless: bool = True
    publish_timeout_ms: int = 30000

    @field_validator("timezone")
    @classmethod
    def _force_shanghai(cls, v: str) -> str:
        if v != TIMEZONE_REQUIRED:
            raise ValueError(
                f"ADR-013 要求时区必须为 {TIMEZONE_REQUIRED}，收到 {v!r}。"
                "改用 UTC 会让'自然日额度'在北京时间 08:00 重置。"
            )
        return v

    @property
    def llm_key_list(self) -> list[str]:
        return [k.strip() for k in self.llm_keys.split(",") if k.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


class DevConfig(BaseConfig):
    env: str = "dev"


class TestConfig(BaseConfig):
    env: str = "test"
    database_url: str = "sqlite:///:memory:"
    log_json: bool = False
    log_level: str = "WARNING"
    publish_driver: str = "fake"          # 测试永不启动真实浏览器
    csrf_enabled: bool = False            # 既有测试不发令牌；CSRF 行为由专项测试覆盖


class ProdConfig(BaseConfig):
    env: str = "prod"
    log_json: bool = True

    @field_validator("secret_key")
    @classmethod
    def _no_insecure_secret(cls, v: str) -> str:
        if v == INSECURE_SECRET:
            raise ValueError("生产环境必须显式设置 GEO_SECRET_KEY，禁止使用默认值")
        return v


_REGISTRY: dict[str, type[BaseConfig]] = {"dev": DevConfig, "test": TestConfig, "prod": ProdConfig}


def load_config(env: str | None = None) -> BaseConfig:
    """按 GEO_ENV 选择配置类；未知环境 fail fast。"""
    key = (env or os.environ.get("GEO_ENV") or "dev").strip().lower()
    if key not in _REGISTRY:
        raise ValueError(f"未知环境 {key!r}，可选：{sorted(_REGISTRY)}")
    return _harden_secret(_REGISTRY[key]())


def _harden_secret(cfg: BaseConfig) -> BaseConfig:
    """会话密钥绝不沿用公开默认常量。

    Flask 的会话 Cookie 只由 SECRET_KEY 签名、且存在客户端：沿用仓库里的
    `dev-insecure-change-me` 等于任何人都能伪造 `{"uid": ...}`（进而
    admin 跨租户）。test 允许（内存库、无真实凭证）；其他环境一律换成
    随机值并告警 —— 代价只是重启后需重新登录（桌面形态由 autologin 兜底）。
    """
    if cfg.secret_key != INSECURE_SECRET or cfg.env == "test":
        return cfg
    cfg.secret_key = secrets.token_urlsafe(32)
    logger.warning("session_secret_generated（未设 GEO_SECRET_KEY，已改用随机值）",
                   extra={"event": "session_secret_generated", "env": cfg.env})
    return cfg


@lru_cache(maxsize=1)
def get_config() -> BaseConfig:
    return load_config()
