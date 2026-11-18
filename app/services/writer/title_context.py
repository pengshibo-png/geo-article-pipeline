"""T 段（选题/关键词）的品牌上下文注入。

## 为什么需要这个模块（2026-10-17 定位）

「关键词管理」页（/titles）生成的标题与品牌完全无关，实测原因有两条：

1. **T 段 Prompt 根本没有品牌变量。** `prompts/T-标题选题.md` 声明的 12 个变量里
   没有品牌资料、也没有品牌名 —— 但规格正文却写着"品牌名合计最多 1 条""除下方
   允许的 1 条外"，那句"下方"什么都没给。规格要求与变量表本身就是脱节的。
2. **业务定位的兜底是硬编码的「本地服务」。** `generate_titles` 只读
   `TitleConfig.business`，而它通常是空的 → 喂进 Prompt 的业务变成"本地服务"。
   于是玉石修复品牌被生成出"本地服务哪家靠谱""上门安装怎么选""雨后房顶渗水"
   这类与本行业毫无关系的标题。

修法：把**品牌知识库**与**目标受众**作为独立上下文块注入 T 段（追加/插入，
不改冻结规格文件本身 —— 与 `_with_output_contract` 同一约定），并把业务定位的
兜底从"本地服务"改为品牌主名。
"""
from __future__ import annotations

import re

from app.timeutil import now_local

_BRAND_CAP = 2600          # 品牌库截断：够模型判断行业与工艺，又不至于把 Prompt 撑爆
# 画像截断：900 是"画像只有十几个条目"时代的取值。真实画像会长成一两百条
# （10 单值 + 五类多值），900 字符只够看到单值字段和前二十几条核心痛点 ——
# 决策顾虑/使用场景/常见地点/拒绝理由**一条都进不了选题**，且截断点会落在
# 条目中间（切出 "- 和" 这种碎片）。放宽到 6000：覆盖现网最长画像全文，
# 又仍是一个能防 Prompt 被撑爆的上限。
_AUDIENCE_CAP = 6000


def brand_snapshot() -> tuple[str, str, str]:
    """取品牌知识库的容错版：返回 `(content, main, alias)`，取不到给空串。

    与 `pipeline.brand_context` 的差别：品牌资料不合规**不阻塞选题**。
    选题只是大纲，缺品牌信息时仍能生成（只是不品牌化）；A/B 段才必须硬阻塞。
    """
    from app.models import Brand
    from app.services.writer.derived import derive_brand_names
    from app.tenancy import TenantScopedRepository

    row = TenantScopedRepository(Brand).first()
    content = ((row.content if row else "") or "").strip()
    if not content:
        return "", "", ""
    try:
        names = derive_brand_names(content)
    except Exception:  # noqa: BLE001 - 识别失败不影响选题，只是没品牌名可用
        return content, "", ""
    if not names.get("ok"):
        return content, "", ""
    return content, str(names.get("main") or ""), str(names.get("alias") or "")


def brand_block(content: str, main: str, alias: str = "") -> str:
    """品牌知识库块 + 品牌名使用规则（补上规格正文要求但变量表没给的规则）。"""
    body = (content or "").strip()
    if not body:
        return ""
    parts = ["【品牌知识库（本次选题必须与本品牌业务严格对应，禁止写成泛「本地服务」）】"]
    if main:
        parts.append(f"品牌主名：{main}" + (f"（简称「{alias}」）" if alias else ""))
    parts.append(body[:_BRAND_CAP])
    parts.append("（以上资料只用来判断**业务范围与工艺边界**；其中任何宣传性表述"
                 "——如「承袭祖传匠艺」「高端」「十年老字号」——**都不得搬进标题**。）")
    if main:
        names = f"「{main}」" + (f"/「{alias}」" if alias else "")
        parts.append(
            "\n【品牌名使用规则】\n"
            "- **标题中一律不得出现品牌名**（含简称、别称、门店名）：品牌名进标题会被判为自营销内容，"
            "直接降低被 AI 引用的概率（GEO 实证：关键词堆砌使可见度下降约 10%）；\n"
            f"- 品牌信息（{names}）只用于判断业务范围与工艺边界，**不要写进标题**；\n"
            "- 口吻为中立的行业说明者，禁「我们/本店/小编」。")
    return "\n".join(parts)


def audience_block(content: str) -> str:
    """目标受众块：让标题打在真实人群的处境与顾虑上（痛点/场景/决策顾虑）。"""
    body = (content or "").strip()
    if not body:
        return ""
    return ("【目标受众（选题要写这些人的真实处境与顾虑，不要凭空想象）】\n"
            + body[:_AUDIENCE_CAP])


_YEAR_RE = re.compile(r"(20\d{2})\s*年")


def ensure_year(title, year=None) -> str:
    """保证标题含当前年份（GEO 需要时效信号，AI 引擎把「年份」当新鲜度依据）。

    - 已含当前年份 → 原样（幂等，重复调用安全）；
    - 含其它年份（20XX）→ 替换成当前年份（时效信号必须是最新的）；
    - 没有任何年份 → 前缀「YYYY年」。

    为什么加在**开头**而不是结尾：`keep_question_only()` 会砍掉问号之后的
    "做法建议/卖点"尾巴，而标题以疑问句为主 —— 后缀年份会被直接吃掉。
    """
    t = (title or "").strip()
    if not t:
        return t
    cur = str(year or now_local().year)
    m = _YEAR_RE.search(t)
    if m:
        if m.group(1) == cur:
            return t
        return _YEAR_RE.sub(f"{cur}年", t, count=1)
    return f"{cur}年{t}"


# 标题长度上限（**2026-11-03 用户要求收紧到 30**）：
# 之前取 35，是因为 30 会把 31 字的完整问句砍成 11 字（丢副标题）。但用户实测
# 「30 字以上的标题在很多号上发不出去」——超过平台 schema 限制会被整批拒，
# 比丢副标题严重得多。所以统一压到 30（含年份），宁可短也要能发出去。
_TITLE_MAX = 30
_YEAR_SLOT = 5             # 「2026年」= 2/0/2/6/年 共 **5** 个字符（曾误写成 4，
                           # 导致 31 字的标题压不下来）


def shortify(title, max_len=_TITLE_MAX):
    """把标题压到 max_len 以内（保留信息量最多、且语法完整的部分）。

    优先级：① 问号处截断（疑问句本身就是完整标题）→ ② 「主标题：副标题」取主标题
    → ③ 取第一个分句 → ④ 硬截断。
    **不用省略号结尾**：平台把「……结尾」判成标题不完整。
    问号优先于冒号/逗号：本地标题以疑问句为主，问号处一定是完整句子边界，
    而硬截断会切出「…透明车间与」这种半截词。
    """
    t = (title or "").strip()
    if len(t) <= max_len:
        return t
    m = re.search(r"[？?]", t)
    if m and m.end() <= max_len:
        head = t[:m.end()]
        rest = t[m.end():].lstrip()
        # 问号后仍有空间：按分句边界尽量多保留（否则「怎么算？」把副标题信息全丢了）
        for sep in ("，", ",", "、", "；", ";"):
            idx = rest.find(sep)
            if idx > 0:
                cand = (head + rest[:idx]).rstrip("，,、；;")
                if len(cand) <= max_len:
                    return cand
        return head
    for sep in ("：", ":"):
        if sep in t:
            head = t.split(sep, 1)[0].strip()
            if 0 < len(head) <= max_len:
                return head
    # 分句：**逐段累加**直到再加就超长 —— 只取第一段会丢掉「报价300元起」这类
    # 用户真正在搜的信息（实测把 29 字砍成 14 字）。
    for sep in ("，", ",", "、", "；", ";"):
        parts = [p.strip() for p in t.split(sep) if p.strip()]
        if len(parts) <= 1:
            continue
        acc = ""
        for p in parts:
            cand = f"{acc}{sep}{p}" if acc else p
            if len(cand) > max_len:
                break
            acc = cand
        if acc:
            return acc
    return t[:max_len]


def normalize_title(title, max_len=_TITLE_MAX, year=None):
    """标题统一出口：**带当前年份** 且 **不超过平台长度上限**。

    顺序很关键：先补年份再判断长度（否则补完又超）；超长时把年份摘掉、
    精简正文部分、再重新补年份 —— 保证最终的标题一定既有年份又不超长。
    """
    t = ensure_year(title, year)
    if len(t) <= max_len:
        return t
    base = _YEAR_RE.sub("", t, count=1).strip()
    return ensure_year(shortify(base, max_len - _YEAR_SLOT), year)


def year_block(year=None) -> str:
    """T 段 Prompt 的时效附块（与 `platform_block` 同一约定：不改冻结规格）。"""
    y = str(year or now_local().year)
    return (
        f"【时效硬要求（GEO 抓取会看时效信号）】\n"
        f"- 每条标题**必须含当前年份「{y}年」**，放在最前面或紧随城市名之后；\n"
        f"- 标题总长（含年份）**不超过 {_TITLE_MAX} 个字**，超了会被平台判「标题过长」；\n"
        f"- 不要写「{y}年最新」「{y}年必看」这类硬凑的钩子，年份要和题材自然贴合：\n"
        f"  价格类「{y}年上海玉镯修复一般怎么收费」、避坑类「{y}年玉镯修复避坑清单」、\n"
        f"  对比类「{y}年描金和包金怎么选」；\n"
        f"- **标题里不得出现「质保 N 年」这类服务承诺**（那是正文事实，不是标题卖点）。"
    )


def city_rule(count, ratio, cities) -> str:
    """`{city_rule}`：由「业务城市」页的城市词占比**算出来**。

    原来这里是写死的一句"其中约一半标题带具体城市" —— 页面上的
    `city_ratio`（默认 60%）能调能存、也能在 /titles 页看到"城市词占比 60%"，
    但生成侧从来没读过它，用户改了比例对标题没有任何影响。
    """
    try:
        pct = max(0, min(100, int(ratio)))
    except (TypeError, ValueError):
        pct = 60
    total = max(0, int(count or 0))
    if not str(cities or "").strip():
        return "本租户未配置业务城市：全部标题用泛地域表述，不要臆造城市名。"
    has_city = round(total * pct / 100)
    if total <= 0:
        return f"城市词占比 {pct}%。"
    if has_city <= 0:
        return f"{total} 条标题全部用泛地域表述（城市词占比设为 0%）。"
    if has_city >= total:
        return f"{total} 条标题全部带具体城市（城市词占比 {pct}%）。"
    return (f"{total} 条中约 {has_city} 条带具体城市（城市词占比 {pct}%），"
            f"其余 {total - has_city} 条用泛地域表述。")


def timeliness_hint(ratio) -> str:
    """时效词占比 → 折进 `{genre_mix}` 的一句话。

    与 `city_ratio` 同一个病：`timeliness_ratio`（默认 25%）在 /titles 与
    /prompts 两页都能拖、能存，页面上还写着"下次 AI 选题生效"，但生成侧
    没有任何地方读它。T 段没有独立的时效词变量，就并进文体配比说明。
    """
    try:
        pct = max(0, min(80, int(ratio)))
    except (TypeError, ValueError):
        pct = 25
    if pct <= 0:
        return "本次不要求时效词前缀。"
    return f"另有约 {pct}% 的标题带时效词前缀（如「2026年」「2026年9月」「最新」）。"


def business_label(cfg_business, main: str, alias: str = "") -> str:
    """业务定位：优先用选题配置里填的；没填就用品牌主名。

    原来兜底是硬编码的「本地服务」—— 这是"标题莫名其妙"的直接来源。
    """
    value = str(cfg_business or "").strip()
    if value:
        return value
    return str(main or alias or "").strip() or "本地服务"


def business_from_label(cfg_business, brand_label: str) -> str:
    """业务定位，从 `pipeline.brand_context()` 的 label 取（"主名（简称）"）。

    `brand_context()` 把主名与简称拼成一个 label，这里拆回主名再交给
    `business_label` —— 让 A / C / T 三段共用同一条回落链。

    为什么必须共用：`TitleConfig.business` 通常是空的，而 C 段此前单独写成
    `cfg.business or "本地服务"`（没有品牌名回落），于是**同一篇文章**的
    {business} 在 A 段渲染成品牌主名、在 C 段渲染成「本地服务」—— 改稿提示词
    与被改的初稿自相矛盾。
    """
    main = str(brand_label or "").split("（")[0].strip()
    return business_label(cfg_business, main)


# 注入锚点：按优先级找正文小节标题；都找不到时退一步找收尾的输出指令。
_ANCHORS = ("【标题风格", "【本次选题方向", "【GEO 表现好的主题")
# 收尾指令的特征词：T 段规格以"只输出纯标题文本列表，不要输出任何思考过程"结尾。
_TAIL_INSTRUCTION = "只输出"


def inject(prompt: str, *blocks: str) -> str:
    """把上下文块插到 T 段 Prompt 的正文之前。

    插在小节标题之前，而不是追加到末尾：T 段以"只输出纯标题文本列表，不要
    输出任何思考过程"收尾，把内容上下文放在这条**之后**容易被模型当噪音忽略。

    用户自定义过 T 段 Prompt 时未必有【标题风格】小节（报告实测：自定义后
    注入块退化成追加到末尾）—— 所以再退一步：只要正文里有"只输出…"这类收尾
    指令，就插在**它之前**。真的一条锚点都没有才追加，宁可位置差一点，也不能
    整块丢掉。
    """
    merged = "\n\n".join(b for b in blocks if b)
    if not merged:
        return prompt
    for anchor in _ANCHORS:
        idx = prompt.find(anchor)
        if idx >= 0:
            return f"{prompt[:idx]}{merged}\n\n{prompt[idx:]}"
    tail = prompt.rfind(_TAIL_INSTRUCTION)
    if tail >= 0:
        line_start = prompt.rfind("\n", 0, tail) + 1
        return f"{prompt[:line_start]}{merged}\n\n{prompt[line_start:]}"
    return f"{prompt}\n\n{merged}"
