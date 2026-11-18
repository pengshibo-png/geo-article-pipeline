"""GEO 四引擎独立端点配置（从 model_service 拆出，守 A3 行数预算）。

四引擎（豆包/通义/元宝/DeepSeek）各配各的 base_url+model+key：共用一个
LLM 客户端的话，四份答案其实出自同一模型，监测数据会失真。

未配的引擎 → GEO 监测退回统一 LLM 客户端（行为与配置前一致，不退化）。
"""
from __future__ import annotations

from app.errors import ValidationError
from app.services.model_config import load_overrides

ENGINES = ("doubao", "qwen", "yuanbao", "deepseek")
LABELS = {"doubao": "豆包", "qwen": "通义千问", "yuanbao": "元宝",
          "deepseek": "DeepSeek"}


def _mask(key: str) -> str:
    if not key:
        return ""
    return f"sk-***{key[-4:]}" if len(key) > 8 else "***"


def clean(body: dict, existing: dict) -> dict:
    """校验并归一四引擎段。留空 = 沿用已存 Key；整段留空 = 不配该引擎。"""
    saved = existing.get("engines") or {}
    out = {}
    for name in ENGINES:
        e = body.get(name)
        if not isinstance(e, dict):
            continue
        base_url = str(e.get("base_url") or "").strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ValidationError(f"{LABELS[name]}：base_url 须以 http(s):// 开头")
        key = str(e.get("key") or "").strip() or str((saved.get(name) or {}).get("key") or "")
        model = str(e.get("model") or "").strip()
        if not (base_url or model or key):
            continue
        if key and not base_url:
            raise ValidationError(f"{LABELS[name]}：填了 Key 就需填 base_url")
        out[name] = {"base_url": base_url, "model": model, "key": key}
    return out


def view(overrides: dict) -> dict:
    """GET 响应形状：四引擎各自的掩码视图（key 恒不落前端）。"""
    saved = overrides.get("engines") or {}
    return {name: {**{k: v for k, v in (saved.get(name) or {}).items() if k != "key"},
                   "has_key": bool((saved.get(name) or {}).get("key")),
                   "key_masked": _mask(str((saved.get(name) or {}).get("key") or ""))}
            for name in ENGINES}


def test(body: dict) -> dict:
    """单引擎连通性实测（用表单现值，不落配置）。"""
    name = str(body.get("engine") or "").strip()
    if name not in ENGINES:
        return {"ok": False, "error": f"引擎须为 {'/'.join(ENGINES)}"}
    e = body.get("config") if isinstance(body.get("config"), dict) else body
    saved = (load_overrides().get("engines") or {}).get(name) or {}
    base_url = str(e.get("base_url") or saved.get("base_url") or "").strip()
    key = str(e.get("key") or saved.get("key") or "").strip()
    model = str(e.get("model") or saved.get("model") or "").strip()
    if not base_url:
        return {"ok": False, "error": f"{LABELS[name]}：缺少接口地址 base_url"}
    if not key:
        return {"ok": False, "error": f"{LABELS[name]}：缺少 API Key"}
    try:
        from app.services.llm import build_engine_client

        client = build_engine_client({"base_url": base_url, "model": model, "key": key})
        client.complete([{"role": "user", "content": "回复：OK"}],
                        model=model or None, timeout=20)
        return {"ok": True, "msg": f"{LABELS[name]} 连接成功" + (f"（{model}）" if model else "")}
    except Exception as exc:  # noqa: BLE001 - 如实呈现上游错误
        return {"ok": False, "error": f"{LABELS[name]} 连接失败：{str(exc)[:140]}"}


__all__ = ["ENGINES", "LABELS", "clean", "test", "view"]
