"""抖音图文的**变量渲染**：把页面上承诺的占位符真正替换掉。

页面（client_douyin.html 的三块「变量说明」）对三条指令各承诺了一组占位符：

- 图文文案提示词：`{brand} {city} {phone} {address} {brand_data} {banned}`
- 创意一体化指令：`{n} {copy}`
- 海报提示词兜底池：`{city} {brand} {phone} {service} {price_lines}`

改造前代码只做过 `{city}` 一处替换（`douyin_pack` 里 `str(pr).replace("{city}", city)`），
用户在指令里写 `{brand}` `{phone}` `{address}`，模型拿到的就是**字面量花括号** ——
实测文案里出现过"品牌：{brand}"、"服务行：{address}"这种句子，页面上那三块说明
等于全部失效。本模块把三组变量集中实现，生成侧统一从这里取。

另一个承诺是"品牌名/资料取自品牌资料页 · 多租户完全隔离"：所以品牌名一律取自当前
租户的品牌资料，不留任何硬编码（配图风格里曾硬编码『聚贤阁』与"玉镯/兰花/竹叶"，
多租户下会把别人家的品牌名印到海报上）。
"""
from __future__ import annotations

import re

from app.logging import get_logger

log = get_logger(__name__)

_VAR = re.compile(r"\{([a-z][a-z0-9_]*)\}")

# 三条指令各自可用的变量（页面上列出的就是这些）
COPY_VARS = ("brand", "city", "phone", "address", "brand_data", "banned")
CREATIVE_VARS = ("n", "copy", "brand", "city", "phone", "address", "brand_data",
                 "banned", "service", "price_lines")
IMAGE_VARS = ("city", "brand", "phone", "service", "price_lines", "address")

_PRICE_HINT = ("元", "¥", "￥", "价格", "报价", "费用", "收费", "㎡", "平米", "单价")


def brand_facts(limit: int = 1800) -> tuple[str, str]:
    """返回 (品牌资料全文, 品牌主名)。取不到返回空串，**绝不阻断产线**。"""
    try:
        from app.services.writer.title_context import brand_snapshot

        content, main, _alias = brand_snapshot()
        return (content or "")[:limit], (main or "")
    except Exception as exc:  # noqa: BLE001 - 品牌资料读不到就退化成"无事实稿"
        log.warning("douyin_brand_snapshot_failed", extra={
            "event": "douyin_brand_snapshot_failed", "err": str(exc)[:160]})
        return "", ""


def default_address(city: str, cities) -> str:
    """`{address}` 的默认取值：与页面上那行预览**同一口径**
    （`${city}·全国N城就近上门`，N = 推广城市里去重后的城市数）。"""
    city = str(city or "").strip()
    if not city:
        return ""
    keys = {str(c).split("市")[0].strip() for c in (cities or []) if str(c).strip()}
    return f"{city}·全国{max(1, len(keys))}城就近上门"


def banned_text(cfg: dict) -> str:
    words = [str(w).strip() for w in (cfg.get("banned_words") or []) if str(w).strip()]
    return "、".join(words[:30])


def price_lines(content: str, limit: int = 6) -> str:
    """从品牌资料里挑"价格行"（如「屋顶补漏 80-260元/㎡」）供海报兜底池使用。

    挑不到就返回空串 —— 不编价格（价格是最不能编的一类事实）。
    """
    out: list[str] = []
    for line in (content or "").splitlines():
        s = line.strip().lstrip("#-*•·> ").strip()
        if not s or len(s) > 80 or s.startswith("|"):
            continue
        if any(h in s for h in _PRICE_HINT):
            out.append(s)
        if len(out) >= limit:
            break
    return "；".join(out)


def business_label() -> str:
    """当前租户的业务定位（配置 → 品牌主名 → 空）。与 T/A/C 段同一份来源。"""
    from app.models import TitleConfig
    from app.tenancy import TenantScopedRepository

    _, main = brand_facts()
    try:
        tc = TenantScopedRepository(TitleConfig).first()
        from app.services.writer import title_context

        return title_context.business_label(
            getattr(tc, "business", None), main, "")
    except Exception:  # noqa: BLE001 - 定位取不到就退回品牌主名
        return main


def service_name(copy_text: str) -> str:
    """`{service}`：本篇的主服务名（如「卫生间防水补漏」）。

    页面写的是"从文案识别的主服务名"。模型侧的图纲规划已经会给出建议，这里给
    **提示词兜底池**用的是更稳的两个来源：租户的业务定位 → 文案标题里最靠前的
    业务短语。都取不到就留空（调用方渲染成空串，不会把 `{service}` 发给模型）。
    """
    label = business_label()
    if label:
        return label
    first = str(copy_text or "").strip().splitlines()[0] if str(copy_text or "").strip() else ""
    frag = re.split(r"[？?！!。；;，,、]", first)[0].strip()
    return frag[:16]


def render(text: str, variables: dict, *, label: str = "") -> str:
    """把 `{name}` 替换成 variables 里的值。

    取不到的变量**保留原样**并记一条 warning（与 A/B/C 段的 `custom_text` 渲染同
    口径）：悄悄替换成空串会让"指令写错变量名"变成静默失效，用户看到的是
    "模型不听话"，而真正的原因永远不会出现在任何地方。
    """
    if not text:
        return ""
    unknown = sorted(set(_VAR.findall(text)) - set(variables))

    def _sub(m):
        return str(variables.get(m.group(1), m.group(0)))

    if unknown:
        log.warning("douyin_prompt_unknown_vars", extra={
            "event": "douyin_prompt_unknown_vars", "field": label,
            "vars": unknown, "hint": "这些占位符原样发给了模型，请核对拼写"})
    return _VAR.sub(_sub, text)


def copy_vars(cfg: dict, city: str, brand: str = "", phone: str = "") -> dict:
    """图文文案提示词的变量表（页面「{brand}…{banned}」那一组）。"""
    content, main = brand_facts()
    res = cfg.get("_resolved") or {}
    return {
        "brand": brand or res.get("brand") or main,
        "city": city,
        "phone": phone or res.get("phone") or str(cfg.get("phone") or ""),
        "address": str(cfg.get("address") or "").strip()
                   or default_address(city, res.get("cities") or cfg.get("cities") or []),
        "brand_data": content,
        "banned": banned_text(cfg),
    }


def visual_vars(cfg: dict, city: str, copy_text: str, n: int,
                brand: str = "", phone: str = "") -> dict:
    """创意一体化指令 / 海报兜底池共用的变量表（多了 {n} {copy} {service} {price_lines}）。"""
    v = copy_vars(cfg, city, brand, phone)
    content, _ = brand_facts()
    v.update({"n": n, "copy": copy_text, "service": service_name(copy_text),
              "price_lines": price_lines(content)})
    return v