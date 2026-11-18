"""GEO 监测 · 虚拟收录查询（模拟各平台回答）—— 监测模块的底层逻辑。

思路取自《GEO 诊断专家》skill（苍何，`geo-diag-report`）：
没有真实联网搜索时，**不硬调裸模型冒充"真实引用"**，而是**模拟各平台回答**：

1. **平台人设**（platformBias）：给每个引擎一段"性格"，让模型扮演该平台回答问句，
   四个引擎的模拟结果才会彼此不同（而不是同一模型复制四份）；
2. **先验锚定**：提及率按"品牌知名度档位 + 问题类型修正 + 平台间 ±8% 波动"
   约束在合理区间，避免 0%/100% 这类明显失真；
3. **诚实标注**：所有此类数据 `channel='simulate'`，如实标注为"模拟"，绝不与
   真实通道（`api`/`browser`）混算。

将来接入"带 citations 的联网 API"（如 Perplexity/豆包联网）后，真实通道再切回。
"""
from __future__ import annotations

import json
import re

from app.extensions import db
from app.logging import get_logger
from app.models import Brand, GeoResult, Tenant, TitleConfig
from app.tenancy import TenantScopedRepository, current_tenant
from app.timeutil import now_local

log = get_logger(__name__)

# 平台偏好（platformBias）—— 取自 skill references/platform-data.md
ENGINE_PERSONA: dict[str, str] = {
    "doubao": "豆包（偏生活场景选购、大众口碑，口语化，倾向给出具体推荐）",
    "qwen": "通义千问（偏结构化对比，喜欢列点、并列多个选项）",
    "yuanbao": "腾讯元宝（偏全面推荐、百科式综述，先给定义再给榜单）",
    "deepseek": "DeepSeek（偏技术参数与原理对比，重视数据与逻辑推演）",
}

# 知名度档位 → 提及率参考区间（skill platform-data.md「品牌知名度与提及率关系」）
FAME_BANDS: dict[str, tuple[float, float]] = {
    "头部": (0.55, 0.80),
    "腰部": (0.30, 0.55),
    "长尾": (0.08, 0.30),
}

# 平台间合理波动 ±8%（同品牌不同平台不应完全一致）
PLATFORM_JITTER = 0.08

_FENCE_RE = re.compile(r"```(?:json)?|```", re.I)
_JSON_RE = re.compile(r"[\[{].*[\]}]", re.S)


def _extract_json(raw: str):
    text = _FENCE_RE.sub("", raw or "").strip()
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


FAME_SYS = (
    "你是品牌 GEO 诊断专家。根据品牌资料判断该品牌在其（地域/行业）品类里的知名度档位："
    "头部=全国或行业知名、市占高；腰部=区域知名；长尾=本地小众。"
    "只输出 JSON，不要解释。"
)


def classify_fame(llm, brand_content: str, product: str, brand: str) -> str:
    """先给品牌定一个知名度档位（提及率锚定的依据）。失败则按"长尾"兜底。"""
    user = (f"品牌：{brand}\n品类：{product}\n品牌资料：{(brand_content or '')[:2000]}\n"
            '只输出：{"fame":"头部|腰部|长尾","reason":"一句话"}')
    try:
        data = _extract_json(llm.complete(
            [{"role": "system", "content": FAME_SYS}, {"role": "user", "content": user}]))
    except Exception:  # noqa: BLE001 - 定档失败不阻断监测
        data = None
    fame = str((data or {}).get("fame") or "").strip()
    return fame if fame in FAME_BANDS else "长尾"


def build_engine_messages(engine: str, brand: str, product: str,
                          queries: list[str], fame: str) -> list[dict]:
    """构造某个引擎的"模拟回答"提示（人设 + 锚定规则 + 逐问句判定）。"""
    lo, hi = FAME_BANDS.get(fame, FAME_BANDS["长尾"])
    persona = ENGINE_PERSONA.get(engine, engine)
    qs = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(queries))
    sys = (
        f"你在模拟 AI 搜索平台「{persona}」回答用户提问时的倾向。\n"
        f"请判断：对本品类的问题，这个平台**会不会提到品牌「{brand}」**，"
        f"若提到，大致排在第几位（从 1 开始）。\n"
        "判定规则：\n"
        f"- 该品牌知名度档位：{fame}（本档位提及率参考区间 "
        f"{int(lo * 100)}%~{int(hi * 100)}%）\n"
        "- 问题类型：推荐类（XX哪个好）提及率偏高；评测类（XX怎么样）略高；"
        "对比类居中；负面类（XX有什么问题）偏低\n"
        "- 不同平台之间提及率应有 ±8% 的合理波动，不要千篇一律\n"
        "- **未提及时 rank 必须为 null**（不要编造 0）\n"
        "只输出 JSON 数组，不要任何解释文字或代码块。"
    )
    user = (f"品类：{product}\n品牌：{brand}\n逐条判定以下问题：\n{qs}\n\n"
            '只输出：[{"i":1,"mentioned":true,"rank":2}, ...]')
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def parse_engine_verdicts(raw: str) -> dict[int, dict]:
    """模型输出 → {问句序号(从1起): {"mentioned": bool, "rank": int|None}}。"""
    data = _extract_json(raw)
    out: dict[int, dict] = {}
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        mentioned = bool(item.get("mentioned"))
        rank = item.get("rank")
        try:
            rank = int(rank) if (rank is not None and mentioned) else None
        except (TypeError, ValueError):
            rank = None
        out[i] = {"mentioned": mentioned, "rank": rank}
    return out


# ── 采样辅助（从 geo_monitor 下沉，A3 行数预算）──────────────────────────────


def _brand_name(tid: str) -> str:
    """品牌主名（解析失败退回租户名）。"""
    from app.services.writer.derived import derive_brand_names

    brand_row = TenantScopedRepository(Brand).first()
    content = (brand_row.content if brand_row else "") or ""
    tenant = db.session.get(Tenant, tid)
    brand = (tenant.name if tenant else tid) or ""
    try:
        names = derive_brand_names(content)
        if names["ok"]:
            brand = str(names["main"])
    except Exception as exc:  # noqa: BLE001 - 主名解析失败退回租户名（监测仍可用）
        log.warning("geo_brand_name_fallback", extra={
            "event": "geo_brand_name_fallback", "err": str(exc)[:120]})
    return brand


def _product() -> str:
    """品类/业务（用于模拟提示；缺省空串，问句本身已含品类信息）。"""
    cfg = TenantScopedRepository(TitleConfig).first()
    return str(getattr(cfg, "business", None) or "").strip()


def sample_engine(llm, engine: str, queries: list[str], brand: str,
                  product: str, fame: str) -> list[GeoResult]:
    """让模型扮演该平台，对全部问句逐条判定"是否提及 / 排第几"，落 GeoResult 行。

    `channel='simulate'`：明确标注这是**模拟**，不是真实引用；
    模拟通道不产生真实链接，故 `cited_lv` 恒为 False（被引只能靠真实通道）。
    """
    msgs = build_engine_messages(engine, brand, product, queries, fame)
    verdicts: dict = {}
    err: str | None = None
    try:
        verdicts = parse_engine_verdicts(llm.complete(msgs))
    except Exception as exc:  # noqa: BLE001 - 整引擎失败 → 该引擎本轮记为 error
        err = str(exc)[:500]
    out: list[GeoResult] = []
    for i, q in enumerate(queries, start=1):
        v = verdicts.get(i) or {}
        g = GeoResult(query=q[:255], engine=engine, channel="simulate",
                      mentioned=bool(v.get("mentioned")), rank=v.get("rank"),
                      cited_lv=False, cited_url=None,
                      sampled_at=now_local(), tenant_id=current_tenant())
        if err:
            g.error = err
        out.append(g)
    return out


__all__ = ["ENGINE_PERSONA", "FAME_BANDS", "PLATFORM_JITTER",
           "classify_fame", "build_engine_messages", "parse_engine_verdicts",
           "_brand_name", "_product", "sample_engine"]
