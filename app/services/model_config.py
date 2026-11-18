"""模型在线配置存储（服务层；`data/model_config.json`）。

为什么放服务层：build_llm_client（services/llm）要读它，不能反向依赖蓝图。
蓝图侧 `config/model_service.py` 只做校验/掩码/测试连接，落盘经本模块。

生效链：**在线覆盖（本文件）> GEO_* 环境变量**。重启保留（写磁盘）。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from flask import current_app

from app.logging import get_logger

log = get_logger(__name__)
_LOCK = threading.Lock()


def config_path() -> Path:
    return Path(current_app.config["DATA_ROOT"]) / "model_config.json"


def load_overrides() -> dict:
    try:
        p = config_path()
    except RuntimeError:
        # 无应用上下文（脚本/单测裸装配）：在线覆盖无从读起，环境变量兜底
        return {}
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("model_config.json 解析失败，按空覆盖处理")
        return {}
    return data if isinstance(data, dict) else {}


def save_overrides(data: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def llm_override() -> dict:
    ov = load_overrides().get("llm")
    return dict(ov) if isinstance(ov, dict) else {}


def image_override() -> dict:
    ov = load_overrides().get("image")
    return dict(ov) if isinstance(ov, dict) else {}


def engine_override(name: str) -> dict:
    """某个 GEO 引擎的在线配置（{"base_url","model","key"}）。未配 → {}。"""
    ov = (load_overrides().get("engines") or {}).get(name)
    return dict(ov) if isinstance(ov, dict) else {}


def effective_engine(name: str) -> dict | None:
    """引擎级最终配置：有 base_url 且 有 key 才算可用（否则 None → 用统一客户端）。

    GEO 监测的四引擎（doubao/qwen/yuanbao/deepseek）各问各的模型才有意义；
    共用一个 LLM 客户端的话，四份答案其实出自同一模型。
    """
    ov = engine_override(name)
    base_url = str(ov.get("base_url") or "").strip()
    key = str(ov.get("key") or "").strip()
    if not base_url or not key:
        return None
    return {"base_url": base_url, "model": str(ov.get("model") or "").strip(),
            "key": key}


def effective_llm(settings) -> dict:
    """合并在线覆盖与环境变量，得到 build_llm_client 应使用的最终 LLM 配置。"""
    base = {
        "provider": getattr(settings, "llm_provider", "fake") or "fake",
        "base_url": getattr(settings, "llm_base_url", "") or "",
        "model": getattr(settings, "llm_model", "") or "",
        "keys": list(getattr(settings, "llm_key_list", []) or []),
        "max_retries": int(getattr(settings, "llm_max_retries", 3)),
        "cooldown": float(getattr(settings, "llm_cooldown_seconds", 60)),
    }
    ov = llm_override()
    if not ov:
        return base
    if ov.get("provider"):
        base["provider"] = ov["provider"]
    if ov.get("base_url"):
        base["base_url"] = ov["base_url"]
    if ov.get("model"):
        base["model"] = ov["model"]
    if ov.get("key"):
        base["keys"] = [ov["key"]]
    return base
