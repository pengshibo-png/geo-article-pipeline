"""配置分层与 fail-fast 校验（ADR-013 时区 / 生产 secret）。"""
from __future__ import annotations

import pytest

from app.config import INSECURE_SECRET, load_config


def test_timezone_must_be_shanghai(monkeypatch):
    """ADR-013：用 UTC 会让"自然日额度"在北京时间 08:00 重置 —— 必须直接失败。"""
    monkeypatch.setenv("GEO_TIMEZONE", "UTC")
    with pytest.raises(Exception):
        load_config("dev")


def test_default_timezone_ok(monkeypatch):
    monkeypatch.setenv("GEO_TIMEZONE", "Asia/Shanghai")
    cfg = load_config("test")
    assert cfg.timezone == "Asia/Shanghai"


def test_prod_rejects_insecure_secret(monkeypatch):
    monkeypatch.setenv("GEO_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("GEO_SECRET_KEY", INSECURE_SECRET)
    with pytest.raises(Exception):
        load_config("prod")


def test_prod_accepts_explicit_secret(monkeypatch):
    monkeypatch.setenv("GEO_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("GEO_SECRET_KEY", "a-proper-production-secret")
    assert load_config("prod").secret_key.startswith("a-proper")


def test_unknown_env_fails_fast():
    with pytest.raises(ValueError):
        load_config("staging-typo")


def test_llm_key_list_parsing(monkeypatch):
    monkeypatch.setenv("GEO_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("GEO_LLM_KEYS", " k1 , k2 ,, k3 ")
    assert load_config("test").llm_key_list == ["k1", "k2", "k3"]
