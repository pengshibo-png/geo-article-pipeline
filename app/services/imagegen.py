"""生图通道（未配置则内置演示出图）。

与 services/publish/drivers.py 同构：真实通道走 model_config 的 image 段
（系统设置页在线配置），离线/未配置走 **MockProvider** —— 生成
确定性占位图（data URI SVG，竖版 3:4 图文尺寸），让抖音图文产线**离线全流程可跑**。

支持的生图协议（image.protocol）：
- ``mock``（内置演示占位，离线）
- ``openai-images``：任意 OpenAI 兼容 /images/generations 端点
- ``sensenova``：**内置生图**（兼容 OpenAI 图像协议；端点与默认模型由本模块常量
  给出，不向界面回显供应商信息）

providers()：供 /api/douyin/image-providers 渲染渠道下拉 —— mock 恒在，
已配置的协议按其 id 列出（模型名取自在线配置的 model 字段）。
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

from flask import current_app

from app.logging import get_logger
from app.platform import net
from app.services.model_config import image_override

log = get_logger(__name__)

_TIMEOUT = 90.0
# 瞬时故障重试：一条内容包要串行出 N 张图，上游被连打若干次后常把后续请求
# hold 住，直到读超时（实测连续 3 张时第 2、3 张开始超时）。重试是唯一能
# 把这类抖动挡在用户之外的手段。
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = (2.0, 8.0)   # 递增退避：给上游队列恢复窗口
MOCK_ID = "mock"

# 内置生图（兼容 OpenAI 图像协议）：POST {BASE}/images/generations
# 鉴权：Authorization: Bearer <key>；端点与默认模型见下方常量。
SENSENOVA_ID = "sensenova"
SENSENOVA_BASE = "https://token.sensenova.cn/v1"
SENSENOVA_MODEL = "sensenova-u1.5-lite"


def conf() -> dict:
    return image_override()


def is_configured() -> bool:
    c = conf()
    if not str(c.get("key") or "").strip():
        return False
    # 内置生图的 base_url 由常量给出，仅需 Key 即视为已配置
    if str(c.get("protocol") or "openai-images") == SENSENOVA_ID:
        return True
    return bool(str(c.get("base_url") or "").strip())


def providers() -> list:
    out = [{"id": MOCK_ID, "name": "内置演示生图（离线占位）",
            "models": ["mock-svg-1"], "has_key": True,
            "note": "不联网，产出统一风格占位图，用于离线体验与压测"}]
    c = conf()
    if is_configured():
        if current_provider() == SENSENOVA_ID:
            # 供应商信息不进界面：渠道名与备注都用中性描述（models 保持真实值供保存用）
            out.insert(0, {"id": SENSENOVA_ID, "name": "在线生图（默认）",
                           "models": [c.get("model") or SENSENOVA_MODEL], "has_key": True,
                           "note": "内置默认模型"})
        else:
            out.insert(0, {"id": "openai-images", "name": "在线生图（OpenAI 兼容）",
                           "models": [c.get("model")] if c.get("model") else [],
                           "has_key": True, "note": str(c.get("base_url") or "")})
    return out


def current_provider() -> str:
    c = conf()
    if not is_configured():
        return MOCK_ID
    if str(c.get("protocol") or "openai-images") == SENSENOVA_ID:
        return SENSENOVA_ID
    return "openai-images"


def _mock_image(prompt: str, size: str, salt: str = "") -> str:
    """确定性占位图：按 prompt(+salt) 短哈希上色，返回 data URI。

    `salt` 让**同一个 prompt 在不同内容包里产出不同占位图**：mock 路径原先只按
    prompt 哈希上色，而图文产线的提示词是轮转复用的，于是跨批次出现字节级相同的
    "重复配图"（实测 46 个包里出现重复图组）。加盐只是改配色种子，不改变
    "同输入同输出"的可复现性（salt 由调用方按包号给出）。
    """
    import hashlib

    seed = f"{salt}|{prompt}"
    h = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16)
    hue = h % 360
    w, _, hh = (size.partition("x")[0], size.partition("x")[1], size.partition("x")[2]) or ("768", "", "1024")
    w, hh = _safe_int(w, 768), _safe_int(hh, 1024)
    label = prompt[:18].replace("&", "").replace("<", "")
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{hh}'>"
           f"<rect width='100%' height='100%' fill='hsl({hue},45%,88%)'/>"
           f"<rect x='8%' y='8%' width='84%' height='55%' rx='12' fill='hsl({hue},40%,72%)'/>"
           f"<text x='50%' y='80%' font-size='{int(w*0.06)}' text-anchor='middle' "
           f"fill='hsl({hue},30%,30%)' font-family='sans-serif'>{label}</text></svg>")
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _safe_int(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


class ImageError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        # retryable=False 是**确定性拒绝**（401 Key 错 / 400 参数不合法 / 余额不足），
        # 重试只会重复烧时间；只有超时、连接中断、429、5xx 才值得再试。
        self.retryable = retryable


def generate(prompt: str, *, size: str = "1024x1024", provider: str | None = None,
             salt: str = "", model: str | None = None) -> str:
    """返回图片引用（http URL 或 data URI）。provider 缺省按已配置自动选。

    `salt` 仅作用于内置占位图（让同 prompt 在不同内容包里配色不同），
    在线生图不受影响 —— 上游返回的是真实图片，本就各不相同。

    `model`：**本产线**指定的生图模型（抖音产线配置里的"生图模型"）。以前这个
    配置项是死配置 —— 页面存得下、上游却始终用系统设置里的 model 字段，用户
    填了模型名毫无作用。为空则仍用系统设置（不改变原有的"跟随系统"语义）。
    """
    pid = provider or current_provider()
    if pid == MOCK_ID or not is_configured():
        return _mock_image(prompt, size, salt)
    c = conf()
    if pid == SENSENOVA_ID:
        return _sensenova_images(prompt, size or c.get("size") or "1024x1024", c, model)
    return _openai_images(prompt, size or c.get("size") or "1024x1024", c, model)


def _post_images(url: str, payload: bytes, key: str) -> dict:
    """POST 一次 /images/generations，返回解析后的 JSON。

    异常一律包成 ImageError 并**标注是否可重试**：调用方（_with_retry）据此决定
    是再试一次还是立刻放弃。
    """
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"}, method="POST")
    try:
        # 走 net.urlopen：忽略注册表系统代理（VPN 代理会断 TLS，见 platform/net.py）
        with net.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read(160).decode("utf-8", "replace")
        raise ImageError(f"生图 HTTP {exc.code}：{body}",
                         retryable=exc.code == 429 or exc.code >= 500) from exc
    except Exception as exc:
        # read operation timed out / ConnectionReset / TLS 抖动 —— 典型瞬时故障
        raise ImageError(f"生图连接失败：{str(exc)[:160]}", retryable=True) from exc


def _with_retry(call, label: str) -> str:
    """瞬时故障重试（次数见 _RETRY_ATTEMPTS），退避递增。

    每次重试都落一条 WARNING 日志：否则"重试救回来了"在磁盘上完全看不到，
    出了慢/贵的问题无法归因。
    """
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return call()
        except ImageError as exc:
            if not exc.retryable or attempt == _RETRY_ATTEMPTS:
                raise
            delay = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
            log.warning("imagegen_retry", extra={
                "event": "imagegen_retry", "channel": label, "attempt": attempt,
                "delay": delay, "err": exc.message[:120]})
            time.sleep(delay)
    raise ImageError("生图失败：重试次数用尽")  # pragma: no cover - 循环内必 return 或 raise


def _sensenova_images(prompt: str, size: str, c: dict,
                      model: str | None = None) -> str:
    """内置生图：POST {SENSENOVA_BASE}/images/generations，Bearer 鉴权。"""
    model = str(model or c.get("model") or SENSENOVA_MODEL)
    payload = json.dumps({"model": model, "prompt": prompt, "size": size}).encode("utf-8")
    url = SENSENOVA_BASE.rstrip("/") + "/images/generations"
    return _with_retry(
        lambda: _extract_image(_post_images(url, payload, c["key"])), SENSENOVA_ID)


def _openai_images(prompt: str, size: str, c: dict, model: str | None = None) -> str:
    payload = json.dumps({"model": model or c.get("model") or "", "prompt": prompt,
                          "size": size, "n": 1}).encode("utf-8")
    url = str(c["base_url"]).rstrip("/") + "/images/generations"
    return _with_retry(
        lambda: _extract_image(_post_images(url, payload, c["key"])), "openai-images")


def _extract_image(data: dict) -> str:
    """从生图响应里提取图片引用，兼容 OpenAI(data[]) 与 images[] 两种结构。"""
    for arr in (data.get("data"), data.get("images")):
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict):
                    u = item.get("url")
                    if u:
                        return str(u)
                    b = item.get("b64_json")
                    if b:
                        return "data:image/png;base64," + str(b)
    if isinstance(data.get("url"), str):
        return data["url"]
    if isinstance(data.get("b64_json"), str):
        return "data:image/png;base64," + data["b64_json"]
    # 200 但结构对不上：可能是上游把错误包在正常响应里 —— 再试一次有机会拿到好结果
    raise ImageError("生图响应缺少 url/b64_json", retryable=True)
