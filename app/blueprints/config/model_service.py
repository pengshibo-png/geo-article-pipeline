"""在线模型配置（系统设置页：不重启用任意 OpenAI 兼容 LLM / 生图 API）。

设计要点：
- **覆盖层**：运行期覆盖写进 `data/model_config.json`（桌面单实例友好、重启保留），
  不改 `.env`；生效链 = 在线覆盖 > GEO_* 环境变量。
- **任意模型**：`provider=openai` + `base_url` 指向任何 OpenAI 兼容端点
  （Moonshot/Zhipu/SiliconFlow/OpenRouter/本地 Ollama/自建 vLLM…），`model` 随端点。
- **Key 安全**：GET 响应里 key 恒为掩码（`sk-***abc`）；提交 key 留空 = 保持原值。
- **测试连接**：`test` 用当前（或表单临时）配置发一次 1-token ping，真实往返；
  LLM 与生图分别可测。
- 仅 admin 可读写（模型配置是运营端权限，客户不可见 —— 见 api 层守卫）。

字段（JSON）：
{
  "llm":   {"provider":"openai","base_url":"...","model":"...","key":"...",
            "temperature":0.7},
  "image": {"protocol":"openai-images","base_url":"...","model":"...",
            "key":"...","size":"1024x1024"}
}
"""
from __future__ import annotations

from flask import current_app

from app.errors import ValidationError
from app.logging import get_logger
from app.blueprints.config import engine_settings  # 四引擎端点（拆出守 A3 行数预算）
from app.services.model_config import (  # 存储在服务层（llm 客户端也读它）
    load_overrides, save_overrides,
)

log = get_logger(__name__)

_PROVIDERS = ("fake", "openai", "deepseek", "doubao", "qwen")
_IMAGE_PROTOCOLS = ("openai-images", "sensenova")

# 内置生图的端点与默认模型（与 services/imagegen.py 保持一致；不向界面回显）
_SENSENOVA_BASE = "https://token.sensenova.cn/v1"
_SENSENOVA_MODEL = "sensenova-u1.5-lite"


def mask_key(key: str) -> str:
    if not key:
        return ""
    return f"sk-***{key[-4:]}" if len(key) > 8 else "***"


def _clean_llm(body: dict, existing: dict) -> dict:
    provider = str(body.get("provider") or "openai").strip().lower()
    if provider not in _PROVIDERS:
        raise ValidationError(f"provider 须为 {'/'.join(_PROVIDERS)}")
    base_url = str(body.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValidationError("base_url 必须以 http:// 或 https:// 开头")
    if provider != "fake":
        if provider == "openai" and not base_url:
            raise ValidationError("provider=openai 需要填写 base_url（任意 OpenAI 兼容端点）")
    key = str(body.get("key") or "").strip()
    if not key:                                  # 留空 = 沿用已存 Key
        key = str((existing.get("llm") or {}).get("key") or "")
    out = {
        "provider": provider,
        "base_url": base_url,
        "model": str(body.get("model") or "").strip(),
        "key": key,
        "temperature": _clamp_f(body.get("temperature"), 0.0, 2.0, 0.7),
    }
    if provider == "fake":
        out["key"] = ""
    return out


def _clean_image(body: dict, existing: dict) -> dict:
    # UI 保存时不带 protocol 字段：保既有 protocol，否则用内置生图（本工程主生图渠道）
    existing_img = existing.get("image") or {}
    protocol = str(body.get("protocol") or existing_img.get("protocol") or "sensenova").strip().lower()
    if protocol not in _IMAGE_PROTOCOLS:
        raise ValidationError(f"生图协议仅支持 {'/'.join(_IMAGE_PROTOCOLS)}")
    if protocol == "sensenova":
        # 内置端点/模型由常量给出，用户无需手填；留空即取默认
        base_url = str(body.get("base_url") or "").strip() or _SENSENOVA_BASE
        model = str(body.get("model") or "").strip() or _SENSENOVA_MODEL
    else:
        base_url = str(body.get("base_url") or "").strip()
        if not base_url.startswith(("http://", "https://")):
            raise ValidationError("生图 base_url 必填且须以 http(s):// 开头")
        model = str(body.get("model") or "").strip()
        if not model:
            raise ValidationError("生图 model 必填（如 sd-xl / dall-e-3）")
    key = str(body.get("key") or "").strip()
    if not key:
        key = str(existing_img.get("key") or "")
    if not key:
        raise ValidationError("生图 API Key 必填")
    return {"protocol": protocol, "base_url": base_url, "model": model, "key": key,
            "size": str(body.get("size") or "1024x1024")[:16]}


def _clamp_f(value, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _clean_yxer(body: dict, existing: dict) -> dict:
    use_mock = bool(body.get("use_mock"))
    base_url = str(body.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValidationError("蚁小二接口地址须以 http(s):// 开头")
    token = str(body.get("token") or "").strip()
    if not token:
        token = str((existing.get("yxer") or {}).get("token") or "")
    # CLI 模式（2026-10-14）：API Key 即全部所需，端点由 yxer CLI 自行管理；
    # base_url/各 path 为旧 HTTP 集成的遗留字段，仅兼容保留，不再参与判定。
    if not use_mock and not token:
        raise ValidationError("填写蚁小二 API Key，或勾选演示模式")
    return {"base_url": base_url, "token": token, "use_mock": use_mock,
            "accounts_path": str(body.get("accounts_path") or "/accounts")[:64],
            "publish_path": str(body.get("publish_path") or "/publish")[:64]}


class ModelSettingsService:
    """GET 掩码回显 / POST 覆盖 / 测试连接。"""

    def get(self) -> dict:
        ov = load_overrides()
        env = current_app.config["GEO_SETTINGS"]
        llm = ov.get("llm") or {}
        image = ov.get("image") or {}
        yxer = ov.get("yxer") or {}
        active_llm = llm or {"provider": env.llm_provider, "base_url": env.llm_base_url,
                             "model": env.llm_model,
                             "key": (env.llm_key_list[0] if env.llm_key_list else "")}
        engines = ov.get("engines") or {}
        return {
            "ok": True,
            "llm": {**{k: v for k, v in active_llm.items() if k != "key"},
                    "has_key": bool(active_llm.get("key")),
                    "key_masked": mask_key(str(active_llm.get("key") or "")),
                    "source": "online" if ov.get("llm") else "env"},
            "image": {**{k: v for k, v in image.items() if k != "key"},
                      "has_key": bool(image.get("key")),
                      "key_masked": mask_key(str(image.get("key") or "")),
                      "configured": bool(image)},
            "yxer": {**{k: v for k, v in yxer.items() if k != "token"},
                     "has_token": bool(yxer.get("token")),
                     "token_masked": mask_key(str(yxer.get("token") or "")),
                     "configured": bool(yxer.get("use_mock") or yxer.get("token"))},
            # GEO 四引擎各自端点（没配的引擎也返回空壳，前端留空待填）
            "engines": engine_settings.view(ov),
        }

    def save(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        existing = load_overrides()
        out = dict(existing)
        if isinstance(body.get("llm"), dict):
            out["llm"] = _clean_llm(body["llm"], existing)
        if isinstance(body.get("image"), dict):
            out["image"] = _clean_image(body["image"], existing)
        if isinstance(body.get("yxer"), dict):
            out["yxer"] = _clean_yxer(body["yxer"], existing)
        if isinstance(body.get("engines"), dict):
            out["engines"] = engine_settings.clean(body["engines"], existing)
        if out == existing:
            raise ValidationError("没有需要保存的改动（请提交 llm / image / engines 段）")
        save_overrides(out)
        log.info("模型配置已在线更新", extra={"event": "model_config_saved",
                                             "sections": sorted(body.keys())})
        return {"ok": True, "msg": "已保存并即时生效（写稿用 LLM 下一批立即按新配置跑）"}

    def test_engine(self, body: dict) -> dict:
        """单个 GEO 引擎的连通性实测（用表单现值，不落配置）。"""
        return engine_settings.test(body)

    def reset(self) -> dict:
        save_overrides({})
        return {"ok": True, "msg": "已恢复环境变量默认配置"}

    # ── 测试连接 ────────────────────────────────────────────────────────────

    def test_llm(self, body: dict) -> dict:
        """用表单现值（未填则用已存配置）发一次最小对话往返。

        预设供应商（deepseek/doubao/qwen）base_url 留空时补内置官方地址——
        与 build_llm_client 的装配规则一致（B4 首版漏了这步，测预设必失败）。
        """
        from app.services.llm import _PRESET_URLS

        merged = {**((load_overrides().get("llm") or {})), **{k: v for k, v in body.items()
                  if v not in (None, "")}}
        conf = _clean_llm(merged, load_overrides())
        if conf["provider"] == "fake":
            return {"ok": True, "msg": "fake 演示模型无需连接测试（离线跑通全链路）"}
        if not conf["key"]:
            return {"ok": False, "error": "缺少 API Key"}
        base_url = conf["base_url"] or _PRESET_URLS.get(conf["provider"], "")
        if not base_url:
            return {"ok": False, "error": "该供应商需要填写接口地址 base_url"}
        try:
            from app.services.llm import LLMClient
            from app.services.llm.keypool import KeyPool
            from app.services.llm.providers import OpenAICompatProvider

            provider = OpenAICompatProvider(base_url=base_url, model=conf["model"])
            client = LLMClient(KeyPool([conf["key"]], cooldown=5), provider, max_retries=0)
            reply = client.complete([{"role": "user", "content": "回复：OK"}],
                                    model=conf["model"] or None, timeout=20)
            return {"ok": True, "msg": f"连接成功，模型回复：{str(reply)[:40]}"}
        except Exception as exc:
            return {"ok": False, "error": f"连接失败：{str(exc)[:160]}"}

    def test_image(self, body: dict) -> dict:
        merged = {**((load_overrides().get("image") or {})), **{k: v for k, v in body.items()
                  if v not in (None, "")}}
        conf = _clean_image(merged, load_overrides())
        if not conf["key"]:
            return {"ok": False, "error": "缺少 API Key"}
        return _ping_image_api(conf)

    def test_yxer(self, body: dict) -> dict:
        """连通性：用表单现值经蚁小二 CLI 实测（临时 HOME，不碰真实 CLI 配置）。"""
        from app.services.publish.yxer_cli import probe_api_key

        merged = {**((load_overrides().get("yxer") or {})), **{k: v for k, v in body.items()
                                                              if v not in (None, "")}}
        if not merged.get("token"):
            merged["token"] = str((load_overrides().get("yxer") or {}).get("token") or "")
        conf = _clean_yxer(merged, load_overrides())
        if conf.get("use_mock"):
            return {"ok": True, "msg": "演示模式在线（假云账号可用，正式环境请填真实 API Key）"}
        if not conf["token"]:
            return {"ok": False, "error": "需要蚁小二 API Key"}
        return probe_api_key(conf["token"])


def _ping_image_api(conf: dict) -> dict:
    """POST {base}/images/generations 最小请求，验证端点/Key/模型可用。"""
    import json as _json
    import urllib.error
    import urllib.request

    from app.platform import net   # 忽略系统代理（VPN 代理会断 TLS）

    payload = _json.dumps({"model": conf["model"], "prompt": "a red dot",
                           "size": conf.get("size") or "1024x1024", "n": 1}).encode()
    req = urllib.request.Request(conf["base_url"].rstrip("/") + "/images/generations",
                                 data=payload,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {conf['key']}"})
    try:
        with net.urlopen(req, timeout=60) as resp:
            _json.loads(resp.read().decode("utf-8", errors="replace"))
        return {"ok": True, "msg": "生图端点连接成功"}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"生图端点 HTTP {exc.code}：{exc.read(160).decode('utf-8','replace')}"}
    except Exception as exc:
        return {"ok": False, "error": f"生图连接失败：{str(exc)[:160]}"}


def resolve_llm_conf() -> dict:
    """给 build_llm_client 用的生效配置（online 覆盖 env）；无在线覆盖返回 {}。"""
    ov = load_overrides().get("llm")
    return dict(ov) if isinstance(ov, dict) else {}


def image_conf() -> dict:
    ov = load_overrides().get("image")
    return dict(ov) if isinstance(ov, dict) else {}
