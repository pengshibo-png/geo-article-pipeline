"""品牌资料解析与完整度算法。

**唯一权威来源**：《品牌资料九大分区规范》v1.0（`spec/品牌资料规范/`）
本模块是`check_brand.py` 的运行时版本——**算法逐行对齐，不重新发明**。
`check_brand.py` 是 CI 门禁（离线校验样例），本模块是在线服务（每次保存时算给前端看）。

设计要点：
- 纯函数，无 Flask / 无 DB依赖 → 可单测、可被 celery worker 复用。
- 完整度**不落库**（派生值入库必然与内容不一致），每次读时现算。
- 两个口径：主指标（写全了没）+ 要素指标（写对了没），后者仅作副指标。

**已知口径差异（刻意保留，供前端提示）**：原系统算法D-1 会把「品牌概述」
判为未填（正文无字面"品牌概述"四字→ `indexOf` 返回 -1），真实完整资料因此卡在 89%。
本模块实现修正项 M1（intro 兜底），故主指标为100%。原算法仅在 `completeness_both()`
里并列返回，供运营对照。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging import get_logger
from app.services.brand_derived import derive_brand_names as _derive_names
from app.services.brand_derived import derive_phone as _derive_phone
from app.services.brand_sections import (
    SECTION_ELEMENT_HINTS,
    SECTION_MIN_CHARS,
    SECTION_NAMES,
)

log = get_logger(__name__)

# 分区常量在 app/services/brand_sections.py、派生变量在 brand_derived.py
# （拆分以守A3 单文件 300 行契约）


def _norm(s: str | None) -> str:
    """去【】与所有空白——用于字数与匹配。"""
    return re.sub(r"[【】\s]", "", s or "")


# ── 解析 ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ParsedBrand:
    """品牌资料解析结果。"""

    blocks: dict[str, str]
    order: list[str]
    intro: str
    total_chars: int

    def section(self, name: str) -> str:
        """按修正项M2/M1 取分区正文。

        精确块 → 包含匹配（兼容 `【核心方法·技术·工艺】`）→ 品牌概述用intro 兜底。
        """
        if name in self.blocks and self.blocks[name]:
            return self.blocks[name]
        for t in self.order:
            if name in t and self.blocks.get(t):
                return self.blocks[t]
        if name == "品牌概述" and self.intro.strip():
            return self.intro.strip()
        return ""

    def unknown_sections(self) -> list[str]:
        """非标准分区块（真实资料里出现过）。不阻断，仅提示。"""
        return [t for t in self.order if not any(n in t for n in SECTION_NAMES)]


def parse_brand(text: str | None) -> ParsedBrand:
    """复现原系统 `parse()`：行首 `^【…】$` 开块，首块前为 intro。"""
    raw: dict[str, list[str]] = {}
    order: list[str] = []
    intro: list[str] = []
    cur: str | None = None

    for ln in (text or "").split("\n"):
        m = re.match(r"^【(.+?)】\s*$", ln)
        if m:
            cur = m.group(1)
            if cur not in raw:
                raw[cur] = []
                order.append(cur)
        elif cur is not None:
            raw[cur].append(ln)
        else:
            intro.append(ln)

    blocks = {t: "\n".join(v).strip() for t, v in raw.items()}
    return ParsedBrand(
        blocks=blocks,
        order=order,
        intro="\n".join(intro).strip(),
        total_chars=len(_norm(text)),
    )


# ── 派生变量 ────────────────────────────────────────────────────────────────
# 实现在 app/services/brand_derived.py（拆分以守 A3 单文件 300 行契约）

def derive_phone(text: str | None) -> str:
    """`{phone}` 转发，便于调用方少写一次 import。"""
    return _derive_phone(text)


# ── 完整度 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SectionState:
    """单分区状态。`chars` 为去空白后字数。"""

    name: str
    filled: bool
    chars: int
    min_chars: int
    element_hit: int
    element_total: int
    body: str = field(repr=False, default="")

    @property
    def element_ok(self) -> bool:
        return self.element_total > 0 and self.element_hit == self.element_total


@dataclass(frozen=True)
class Completeness:
    """完整度结果。主指标进环，副指标只展示。"""

    sections: tuple[SectionState, ...]
    filled_count: int
    total: int
    element_hit: int
    element_total: int

    @property
    def percent(self) -> int:
        """主指标：写全了没（进环的数字）。"""
        return round(self.filled_count / self.total * 100) if self.total else 0

    @property
    def element_percent(self) -> int:
        """副指标：写对了没（不参与主环）。"""
        return round(self.element_hit / self.element_total * 100) if self.element_total else 0

    def missing(self) -> list[str]:
        return [s.name for s in self.sections if not s.filled]


def _element_score(name: str, body: str) -> tuple[int, int]:
    pats = SECTION_ELEMENT_HINTS.get(name, ())
    hit = sum(1 for p in pats if re.search(p, body or ""))
    return hit, len(pats)


def compute_completeness(p: ParsedBrand) -> Completeness:
    """修正算法 M1–M4：主指标用分区块阈值判定，另有要素完整度。"""
    states: list[SectionState] = []
    for name in SECTION_NAMES:
        body = p.section(name)
        chars = len(_norm(body))
        minimum = SECTION_MIN_CHARS.get(name, 60)
        hit, total = _element_score(name, body)
        states.append(
            SectionState(
                name=name,
                filled=chars >= minimum,
                chars=chars,
                min_chars=minimum,
                element_hit=hit,
                element_total=total,
                body=body,
            )
        )
    return Completeness(
        sections=tuple(states),
        filled_count=sum(1 for s in states if s.filled),
        total=len(states),
        element_hit=sum(s.element_hit for s in states),
        element_total=sum(s.element_total for s in states),
    )


def completeness_original(text: str | None) -> int:
    """**原系统算法**（含缺陷 D-1/D-4），仅供运营对照展示。

    D-1: `text.find(name)` 找不到字面「品牌概述」→ -1 → 必然漏算
    D-3: 只看关键词后 60 字、门槛仅 >8 字 → 假完整
    """
    hit = 0
    for name in SECTION_NAMES:
        i = (text or "").find(name)
        if i < 0:
            continue
        if len(_norm((text or "")[i + len(name) : i + len(name) + 60])) > 8:
            hit += 1
    return round(hit / len(SECTION_NAMES) * 100)


# ── 对外主入口 ──────────────────────────────────────────────────────────────

def verify_spec_consistency() -> list[str]:
    """校验运行时常量与 S3 冻结的 `brand-sections.json` 是否一致。

    **不做降级、不造副本**——两份规格不一致是必须暴露的缺陷，
    悄悄回退到内置副本只会让"代码改了规格"永远无人发现（G10 的立意）。
    一致时返回空列表；不一致返回差异描述。
    """
    import json

    from app.spec import brand_sections_file

    path = brand_sections_file()
    if not path or not Path(path).is_file():
        return [f"规格文件不可达：{path}"]
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    upstream = [s["name"] for s in spec.get("sections", [])]
    diffs: list[str] = []
    if tuple(upstream) != SECTION_NAMES:
        diffs.append(f"分区名或顺序不一致：JSON={upstream} 代码={list(SECTION_NAMES)}")
    for sec in spec.get("sections", []):
        name = sec.get("name")
        want = sec.get("min_chars")
        got = SECTION_MIN_CHARS.get(name)
        if want is not None and got is not None and want != got:
            diffs.append(f"{name} 门槛不一致：JSON={want} 代码={got}")
    if diffs:
        log.error("品牌分区规格与代码不一致", extra={"event": "brand_spec_drift", "diffs": diffs})
    return diffs


def analyze(text: str | None) -> dict[str, Any]:
    """**品牌资料总分析** —— 写入后与读取时都调它。

    返回可直接喂给前端渲染完整度环 + 九大分区卡的结构。
    """
    parsed = parse_brand(text)
    comp = compute_completeness(parsed)
    names = _derive_names(parsed.section("品牌概述"), parsed.section("资质与认证"))

    return {
        "total_chars": parsed.total_chars,
        "completeness": comp.percent,
        "element_completeness": comp.element_percent,
        "completeness_original": completeness_original(text),
        "filled": comp.filled_count,
        "section_total": comp.total,
        "missing": comp.missing(),
        "order": parsed.order,
        "unknown_sections": parsed.unknown_sections(),
        "derived": {
            "brand_names": {
                "main": names.main,
                "entity": names.entity,
                "alias": names.alias,
                "alias_source": names.alias_source,
                "confirmed": names.confirmed,
                # 主体名与主名不一致 = 阻塞项（brand-names.md §4）
                "entity_mismatch": names.entity_mismatch,
            },
            "phone": derive_phone(text),
        },
        "sections": [
            {
                "name": s.name,
                "filled": s.filled,
                "chars": s.chars,
                "min_chars": s.min_chars,
                "element_hit": s.element_hit,
                "element_total": s.element_total,
                "body": s.body,
            }
            for s in comp.sections
        ],
    }