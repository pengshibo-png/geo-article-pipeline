"""B 段输出的真模型适配层（review.py 拆分，A3 行数预算）。

冻结契约（validate_review.py）**一个字不放松**；本层只做真实 LLM 输出的
格式宽容：维度名/verdict/severity 归一、JSON 常见病修复、location 模糊
定位回原文（定位不到的意见丢弃 —— 防幻觉意图不变）。
"""
from __future__ import annotations

import json
import re

from app.errors import ValidationError
from app.logging import get_logger

log = get_logger(__name__)

_FROZEN_DIMENSIONS = (
    "维度一：事实与专业准确性",
    "维度二：逻辑严密性",
    "维度三：结构完整性与GEO可引用性",
    "维度四：专业深度与信息密度",
    "维度五：品牌融入质量",
    "维度六：文字质量与阅读体验",
    "维度七：合规与风险",
)
# 真实模型常见变体 → 冻结维度的关键词归一（只放宽格式/同义，不改口径）
_DIM_KEYWORDS = (
    ("事实", 0), ("准确", 0), ("逻辑", 1), ("结构", 2), ("可引用", 2),
    ("深度", 3), ("信息密度", 3), ("品牌", 4), ("文字", 5), ("阅读", 5),
    ("合规", 6), ("风险", 6),
)
_FROZEN_VERDICTS = ("可直接发布", "需要修改", "建议重写")
_VERDICT_ALIASES = {"通过": "可直接发布", "合格": "可直接发布", "可直接用": "可直接发布",
                    "小改": "需要修改", "需修改": "需要修改", "建议重写": "建议重写",
                    "重写": "建议重写"}
_SEVERITY_ALIASES = {"高": "严重", "严重": "严重", "重要": "建议", "建议": "建议",
                     "一般": "微调", "轻微": "微调", "低": "微调", "微调": "微调"}


def _normalize_dimension(raw: str) -> str:
    """真模型维度名归一到冻结名：去空白 → 维度号匹配 → 关键词匹配。"""
    text = re.sub(r"\s+", "", str(raw or ""))
    for name in _FROZEN_DIMENSIONS:          # 维度号（"维度三"/"三"）精确归位
        if text and (text in name.replace("：", "") or text == name):
            return name
    head = text.split("：", 1)[-1]
    for name in _FROZEN_DIMENSIONS:
        if head and head in name.replace("：", ""):
            return name
    for kw, idx in _DIM_KEYWORDS:            # 自造名按关键词归位
        if kw in text:
            return _FROZEN_DIMENSIONS[idx]
    return str(raw or "").strip()


def _coerce_score(data: dict) -> None:
    """真模型会把 overall_score 给成字符串/小数（"82" / 82.0 / "82/100"）；
    冻结契约只收真整数。这里只做**类型归一**，不改数值口径 —— 越界、
    verdict↔score 映射冲突仍由契约照常拒（不放松任何冻结规则）。"""
    score = data.get("overall_score")
    if isinstance(score, bool):
        return                      # bool 是 int 子类，但不是分数：交给契约拒
    if isinstance(score, str):
        m = re.search(r"-?\d+", score.replace(",", ""))
        if m:
            data["overall_score"] = int(m.group())
    elif isinstance(score, float) and score.is_integer():
        data["overall_score"] = int(score)


def _normalize_review(data: dict) -> dict:
    """真实模型输出的轻量归一：verdict/severity 别名 + dimension 格式归一 +
    score 类型归一 + null 容器归一。

    只做**格式与别名**的宽容（内容口径仍由冻结契约把关）；归一后仍不合法的
    照常报错 —— 不放松任何冻结规则。
    """
    if not isinstance(data, dict):
        return data
    _coerce_score(data)
    if data.get("issues") is None:
        data["issues"] = []         # null → 空数组（契约要 list，不要 null）
    if data.get("highlights") is None:
        data["highlights"] = []
    verdict = str(data.get("verdict") or "").strip()
    if verdict and verdict not in _FROZEN_VERDICTS:
        for alias, frozen in _VERDICT_ALIASES.items():
            if alias in verdict:
                data["verdict"] = frozen
                break
    for issue in data.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if "dimension" in issue:
            issue["dimension"] = _normalize_dimension(issue["dimension"])
        sev = str(issue.get("severity") or "").strip()
        if sev and sev not in ("严重", "建议", "微调"):
            for alias, frozen in _SEVERITY_ALIASES.items():
                if alias == sev or alias in sev:
                    issue["severity"] = frozen
                    break
    return data


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _salvage_json(text: str) -> str | None:
    """真模型 JSON 的常见病修复：提取 {} 块、去围栏、去尾逗号、
    值内未转义英文引号换成中文引号（结构引号保留）。修不好返回 None。"""
    m = _JSON_BLOCK.search(text or "")
    if not m:
        return None
    body = m.group(0).strip()
    for attempt in (body,
                    _TRAILING_COMMA.sub(r"\1", body)):
        try:
            json.loads(attempt, strict=False)
            return attempt
        except json.JSONDecodeError:
            continue
    # 逐行修"字符串值内的裸引号"：行形如 "key": "…text…" —— 值内 " → ”
    fixed_lines = []
    for line in body.splitlines():
        mm = re.match(r'^(\s*"[^"]+"\s*:\s*")(.*)("\s*,?\s*)$', line)
        if mm and '"' in mm.group(2):
            fixed_lines.append(mm.group(1) + mm.group(2).replace('"', "”") + mm.group(3))
        else:
            fixed_lines.append(line)
    repaired = "\n".join(fixed_lines)
    try:
        json.loads(_TRAILING_COMMA.sub(r"\1", repaired), strict=False)
        return _TRAILING_COMMA.sub(r"\1", repaired)
    except json.JSONDecodeError:
        return None


def _locate_in_article(loc: str, article: str) -> str | None:
    """去空白后在原文中定位模型给的引用片段，返回**原文真实片段**（保住
    "location ∈ 原文"的防幻觉契约，同时宽容模型的改写/换行差异）。"""
    def norm(s: str) -> str:
        return re.sub(r"\s+", "", s or "")

    nl, na = norm(loc), norm(article)
    if len(nl) < 4:
        return None
    i = na.find(nl)
    if i < 0:
        return None
    idxs = [j for j, ch in enumerate(article) if not ch.isspace()]
    end = min(i + len(nl), len(idxs))
    return article[idxs[i]: idxs[end - 1] + 1]


def _fix_locations(data: dict, article: str | None) -> dict:
    """location 模糊定位修复：能映射回原文的替换为原文片段；映射不到的
    整条丢弃（无法定位的意见可能是幻觉，宁可少一条不可编造一条）。

    ★ **丢弃必须留痕**：静默删条会让"严重问题被悄悄删掉"，而页面/报表都看不出来。
    所以丢的条数、严重度、问题摘要一律写日志（可审计）。
    """
    if not article or not isinstance(data.get("issues"), list):
        return data
    kept, dropped = [], []
    for issue in data["issues"]:
        loc = str(issue.get("location") or "").strip().strip('"“”')
        if not loc or loc in article:
            kept.append(issue)
            continue
        fixed = _locate_in_article(loc, article)
        if fixed:
            issue["location"] = fixed
            kept.append(issue)
        else:
            dropped.append({"severity": issue.get("severity"),
                            "location": loc[:60],
                            "problem": str(issue.get("problem") or "")[:80]})
    data["issues"] = kept
    if dropped:
        log.warning("review_issues_dropped", extra={
            "event": "review_issues_dropped", "count": len(dropped),
            "severities": sorted({str(d.get("severity")) for d in dropped}),
            "sample": dropped[:3]})
    return data


# 七维权重（合计 100%）：维度一最高，对齐规格里"维度一权重最高"的口径。
_DIM_WEIGHTS = (0.30, 0.15, 0.20, 0.10, 0.05, 0.08, 0.12)


def _verdict_for(score: int) -> str:
    """分数 → verdict（与冻结映射一致：≥90 可直接发布 / 60-89 需要修改 / <60 建议重写）。"""
    if score >= 90:
        return "可直接发布"
    return "需要修改" if score >= 60 else "建议重写"


def apply_weights(data: dict) -> dict:
    """用**分维度分数**重算总分与 verdict —— 这才是"真正的加权"。

    为什么在代码里算：模型的 `overall_score` 是个整体感觉值，七个维度之间没有
    真实权重关系；加权必须是确定性的、可审计的，不能每篇换一套。

    模型给了完整的 `dimension_scores`（七维各 0-100）就用它算，并把 verdict 一并
    重算（避免"分数与 verdict 不一致"这种无意义重试）；缺项或脏值则保留模型原值，
    由冻结契约照常判定（后向兼容：老样例与测试夹具不必带这个字段）。
    """
    raw = data.get("dimension_scores")
    if not isinstance(raw, dict):
        return data
    scores: dict[str, int] = {}
    for key, value in raw.items():
        name = _normalize_dimension(key)
        if name not in _FROZEN_DIMENSIONS:
            continue
        try:
            scores[name] = max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError):
            return data
    if len(scores) != len(_FROZEN_DIMENSIONS):
        return data
    total = round(sum(scores[name] * w
                      for name, w in zip(_FROZEN_DIMENSIONS, _DIM_WEIGHTS)))
    weighted = int(max(0, min(100, total)))
    data["dimension_scores"] = scores            # 归一成整数，供契约校验
    data["overall_score"] = weighted
    # 七维分同样是**模型自己给的**，加权只是把它自己的细粒度判断整合起来 ——
    # 所以 verdict 一律以加权分为准（模型若给出"维度满分但总分 60"这种自相矛盾，
    # 信更细粒度的七维）。曾试过"取模型与加权的更严者"，但那会破坏这条契约
    # （七维全 100 的稿子被压成 89），且并不能真正拦住问题稿 —— 拦问题稿靠
    # 维度分本身（严重问题会把对应维度打到低分）。
    data["verdict"] = _verdict_for(weighted)
    return data


def parse_review(raw: str, *, article: str | None = None) -> dict:
    """B 段输出解析：必须纯 JSON，且通过冻结契约（含 verdict↔score 映射）。

    真实模型的输出病（围栏/尾逗号/值内裸引号）先做一次**修复尝试**；
    修不好的照常报错 —— 内容口径与冻结规则一律不放松。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        salvaged = _salvage_json(text)
        if salvaged is None:
            raise ValidationError(f"审稿输出不是合法 JSON：{exc}") from exc
        try:
            data = json.loads(salvaged, strict=False)
        except json.JSONDecodeError as exc2:
            raise ValidationError(f"审稿输出不是合法 JSON：{exc2}") from exc2
    data = _normalize_review(data)
    data = apply_weights(data)          # 七维加权重算总分与 verdict（必须早于契约校验）
    data = _fix_locations(data, article)
    from app.services.writer.review import review_contract  # 延迟导入避免循环

    errs = review_contract().validate(data, article)
    if errs:
        raise ValidationError("审稿输出违反冻结契约：" + "；".join(errs))
    return data


# 运行时输出格式强化（追加在 B/C 段 Prompt 末尾；不改冻结规格文件本身）
_OUTPUT_CONTRACT = (
    "\n\n【输出硬性要求】\n"
    "- 只输出一个 JSON 对象，禁止 Markdown 代码块、禁止任何 JSON 之外的文字。\n"
    '- JSON 字符串值内禁止出现未转义的英文双引号 " —— 需要引用时一律用中文引号“”。\n'
    "- 顶层**必须且只能**有下面这 6 个字段，少一个、多一个都不接受：\n"
    "  overall_score（0-100 的整数）、verdict（字符串）、summary（非空字符串，"
    "常被漏写，务必输出）、issues（数组）、highlights（0-3 条字符串数组）、\n"
    "  dimension_scores（对象：七个维度名各打 0-100 的整数分，键名逐字使用维度名）。\n"
    "- **加权口径（总分由程序按此重算）**：维度一 30%、维度二 15%、维度三 20%、"
    "维度四 10%、维度五 5%、维度六 8%、维度七 12%；\n"
    "  overall_score 必须等于这七项按上述权重的加权和（四舍五入取整）。\n"
    '- issues[].dimension 必须逐字使用本指令给出的七个维度名（含"维度X："前缀，无空格）。\n'
    "- issues[].location 必须逐字复制待审文章中连续的一段原文（≥8字，不得改写）。"
)


def _with_output_contract(prompt: str) -> str:
    return prompt if "输出硬性要求" in prompt else prompt + _OUTPUT_CONTRACT


def retry_hint(reason: str) -> str:
    """上一次输出未过契约 → 把**具体错误项**喂回去再试一次。

    实测（2026-10-16）：真模型给出的审稿整体合格，却偶尔漏写 `summary`
    这种单个字段，被契约一票否决后整篇作废。这是**格式问题**，重试即可修；
    内容口径仍完全由冻结契约把关，不放松任何一条。
    """
    return ("\n\n【上一次输出未通过格式校验，请严格修正后重新输出完整 JSON】\n"
            f"具体问题：{reason[:400]}\n"
            "只输出修正后的 JSON 对象本身，不要解释、不要代码块。")
