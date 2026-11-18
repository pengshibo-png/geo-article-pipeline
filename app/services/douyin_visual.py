"""抖音图文的**配图规划**：图必须与文案同一套、同一风格。

## 为什么有这层（2026-10-19）

改造前 `image_prompts` 是 4 条**固定**提示词按 `k % len` 轮转，结果是：

- 图与文案**无关**：文案讲"质保"，图上还是那句"避坑清单卡"
- 风格是**营销海报**（顶部品牌名 + 服务范围 + 电话），不是科普
- 条数一多就重复（同 prompt 复用同一张构图）

现在改成：文案写完之后，**让模型读完这段文案，产出 N 张图各自的"图纲"**，
再把图纲渲染成生图提示词。图序、标题、要点全部来自这段文案 —— 所以是一套的。

## 风格规范（STYLE）来自用户提供的样例图

样例是竖版 9:16 科普信息图：米白/淡绿底 + 金色描边卡片 + 黑色超粗标题 +
对比块（灰底=常态/错，淡绿底=聚贤阁的做法）+ 底部固定服务行。
"""

from __future__ import annotations

import json
import re

from flask import current_app

from app.logging import get_logger

log = get_logger(__name__)

# ── 风格规范：所有图共用的"外观底座"（写图纲时不必重复，渲染时才拼上）──────────
STYLE = (
    "竖版 9:16 科普信息图，米白底(#F5F1E4)或淡绿底(#E9F4E8)，"
    "顶部正中金色品牌名（居中对齐，特大号粗体，字号约为主标题的三分之二、正文的三倍，"
    "占画面宽度 40% 左右，最醒目，下方留白与主标题分隔、不重叠，细金色花纹装饰），"
    "超大黑色粗黑体主标题占画面上三分之一，"
    "正文为 2~4 个白色或浅绿圆角卡片，卡片带 1~2px 金色(#C9A227)描边与轻微内阴影，"
    "标题与要点为黑色(#1A1A1A)粗体，要点小字深灰，"
    "金线分隔线配菱形小装饰，角落点缀与业务相关的淡雅线稿插画，"
    "整体信息密度高、像一页精心排版的科普海报；"
    "禁止：照片写实风格、卡通插画、鲜艳渐变、红色促销标签、夸张阴影"
)

# 底部固定行（样例图每张都有）。**通用口径**：原来写死"本地实体店铺｜全国连锁
# 门店就近承接"，那是原租户（玉石修复）自己的门店事实，换到别的租户就是把别人家的
# 资质印在自家海报上。真实口径由 `{address}`（产线配置的"服务行内容"）驱动，
# 见 `douyin_vars.default_address` 与 douyin_pack 里传入的 footer。
FOOTER = "底部一行小字：服务｜本地实体门店就近承接｜到店不便或外地客户可寄修"

# 四种版式（对应样例的 4 张图）
LAYOUTS = {
    "cover": "封面总纲版：主标题下 3 个编号卡片（圆形数字 + 小标题 + 一句说明），"
             "底部左右两个对比块（左灰底写'大多数人只关心…'，右淡绿底写'内行提醒你问…'）",
    "knowledge": "知识科普版：主标题 + 金色副标题 + 2~3 个浅绿渐变说明卡 + "
                 "一组对比块（左淡粉底红字写被否定的说法并画删除线，右淡绿底写品牌的做法并打绿色✓）"
                 "+ 底部 3 个白色小卡片 + 金黑双色总结句",
    "checklist": "清单版：主标题 + 2~3 个分段（每段「第一看/第二看…」粗体小标题 + 一句说明），"
                 "每段下方 2~4 个浅绿小卡片或金色描边标签列出要点，"
                 "其中一段用左右对比块（灰底写行业常见做法，绿底写本店的做法）",
    "flow": "流程版：顶部两个金色勋章图标卡（左右并排，各配一句资质说明）+ "
            "「先鉴定、后修复」标题下 4 个步骤卡（步骤1~4，用箭头串联）+ "
            "底部一排金色描边标签列出适用工艺",
}

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _facts(cfg: dict, limit: int = 1800) -> str:
    """品牌资料 + 可引用事实库：图上的事实只能来自这里。"""
    from app.services.writer.title_context import brand_snapshot

    try:
        content, main, _alias = brand_snapshot()
    except Exception:  # noqa: BLE001 - 取不到就不注入，不阻断产线
        content, main = "", ""
    parts = []
    if content:
        parts.append(f"【品牌资料（图中事实只能来自这里）】\n{content[:limit]}")
    if main:
        parts.append(f"品牌名：{main}")
    return "\n\n".join(parts)


def plan_pack(cfg: dict, copy_text: str, city: str, n: int) -> dict:
    """读这段文案，产出 `{"slides": [...], "tags": [...]}`。失败返回 `{}`（走兜底池）。

    **指令用「创意一体化指令」（creative_prompt）**：页面上这条指令写的是
    "标签 + 每张海报素材摘录"，可用变量是 `{n}`（配图数量）与 `{copy}`（文案全文）——
    改造前它只被当成一句"主题"拼进文案提示词（`主题：{creative_prompt}`），
    而这里用的却是 `copy_prompt`。于是页面上承诺的"一体化编排"完全没接线。
    """
    from app.services import douyin_vars

    v = douyin_vars.visual_vars(cfg, city, copy_text, n)
    guide = (str(cfg.get("creative_prompt") or "").strip()
             or str(cfg.get("copy_prompt") or ""))
    banned = (f"【禁用词（图中的文字一律不得出现）】{v['banned']}\n"
              if v.get("banned") else "")
    prompt = (
        f"{douyin_vars.render(guide, v, label='creative_prompt')}\n\n"
        f"{_facts(cfg)}\n\n"
        f"【本篇城市】{city}\n"
        f"【本篇图文文案全文（图的标题与要点必须出自这里）】\n{copy_text[:1600]}\n\n"
        f"{banned}"
        f"【任务】为这篇文案规划 **{n} 张**竖版科普信息图，要求图与文案**同一套**：\n"
        "- 第 1 张永远是 cover（封面总纲），概括本篇要讲的几件事；\n"
        "- 其余张按文案自身的结构切分（可依次用 knowledge / checklist / flow / cover），\n"
        "  每张只讲文案里的**一个**部分，不要都讲质保或都讲流程；\n"
        "- 每张的标题与要点必须是**从文案原文里摘**的，不要另起炉灶，也不要写文案没有的信息；\n"
        "- 数字、资质、工艺名、价格一律照抄文案原文，不得改写或编造。\n\n"
        f"【可用版式（role）】{json.dumps(LAYOUTS, ensure_ascii=False)}\n\n"
        "只输出一个 JSON 对象，不要代码围栏、不要解释文字：\n"
        '{"tags":["标签","标签"],"slides":[{"role":"cover","headline":"主标题(18字内)",'
        '"cards":[{"k":"1","t":"小标题","d":"一句话说明"}],'
        '"contrast":{"left":"灰底写的内容","right":"绿底写的内容"},"tail":"底部总结句"}]}\n'
        "字段说明：cover 用 cards(3个)+contrast+tail；knowledge 用 blocks(3条)+contrast"
        "{wrong,right}+tail；checklist 用 sections[{t,d,chips[]}]；flow 用 badges[]+steps[4]+tags[]。\n"
        f"slides 必须是 {n} 个；tags 是本篇的话题标签（3~6 个，不带 #）。"
    )
    try:
        from app.services.llm import build_llm_client

        client = build_llm_client(current_app.config["GEO_SETTINGS"])
        raw = str(client.complete([{"role": "user", "content": prompt}],
                                  # 8000：reasoning 模型会先把预算花在思维链上，
                                  # 4000 实测不够 → finish_reason=length → 图纲写一半
                                  # → 整个规划失败回落兜底（图与文案脱钩）。
                                  max_tokens=8000) or "")
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        try:
            data = json.loads(raw)
        except ValueError:
            m = _JSON_RE.search(raw)
            data = json.loads(m.group(0)) if m else {}
        slides = data.get("slides") or []
        slides = [s for s in slides if isinstance(s, dict) and str(s.get("headline") or "").strip()]
        tags = [str(t).lstrip("#").strip()
                for t in (data.get("tags") or []) if str(t).strip()][:8]
        if slides:
            return {"slides": slides[:max(1, n)], "tags": tags}
    except Exception as exc:  # noqa: BLE001 - 规划失败走兜底池，产线不断流
        log.warning("douyin_visual_plan_failed", extra={
            "event": "douyin_visual_plan_failed", "err": str(exc)[:160]})
    return {}


def plan_slides(cfg: dict, copy_text: str, city: str, n: int) -> list[dict]:
    """图纲列表（`plan_pack` 的兼容入口，既有调用方与测试仍从这里取）。"""
    return list((plan_pack(cfg, copy_text, city, n) or {}).get("slides") or [])


def _brand_label(brand: str) -> str:
    """图上品牌名：优先租户简称（如「聚贤阁」），否则主名截断 12 字。

    只有传入的确实是当前租户主名时才换成简称，避免把别家的品牌名换掉。
    """
    label = str(brand or "").strip()
    if not label:
        return ""
    try:
        from app.services.writer.title_context import brand_snapshot

        _content, bmain, balias = brand_snapshot()
        if balias and bmain and label.startswith(bmain):
            label = balias
    except Exception:  # noqa: BLE001 - 无 app 上下文/取不到就保持原样
        pass
    return re.split(r"[，,。；;：:\s]", label)[0][:12]


def render_prompt(slide: dict, footer: str = FOOTER, brand: str = "",
                  style: str = "") -> str:
    """把一张图纲渲染成生图提示词（风格底座 + 版式 + 内容）。

    `brand` 是**当前租户**的品牌名（原来硬编码 『聚贤阁』—— 多租户下会把别人家的
    品牌名印到自家海报的顶部）。取不到就不写这一行，不编造品牌名。
    `style` 是页面上配置的画风（`cfg["style_prompt"]`）；**空则用内置 STYLE**。
    这也正是「改风格不用改代码」的入口（2026-11-05）。
    """
    role = str(slide.get("role") or "cover").strip()
    layout = LAYOUTS.get(role, LAYOUTS["cover"])
    parts = [str(style or "").strip() or STYLE, f"【版式】{layout}"]
    main = _brand_label(brand)
    if main:
        parts.append(
            f"【顶部正中品牌名（特大号，字号加大：约为主标题 2/3、正文 3 倍，"
            f"宽度占画面 40%）】{main}")

    head = str(slide.get("headline") or "").strip()
    if head:
        parts.append(f"【主标题】{head}")

    def _bullets(items, label):
        """列表项既可能是字符串，也可能是 {"t":..,"d":..}（模型常这样返回）。

        旧实现直接 `str(x)` → 把 dict 原样打印成
        `{'t': '顺石纹走还是横切圈口', 'd': '…'}` 发给生图模型，图上就会出现这串代码。
        """
        out: list[str] = []
        for x in (items or []):
            if isinstance(x, dict):
                t = str(x.get("t") or x.get("title") or "").strip()
                d = str(x.get("d") or x.get("desc") or "").strip()
                text = f"{t}｜{d}" if (t and d) else (t or d)
            else:
                text = str(x).strip()
            if text:
                out.append(text)
        if out:
            parts.append(f"【{label}】" + "；".join(out[:6]))

    def _cards(cards):
        for c in (cards or [])[:4]:
            if isinstance(c, dict) and str(c.get("t") or "").strip():
                k = str(c.get("k") or "").strip()
                t = str(c["t"]).strip()
                d = str(c.get("d") or "").strip()
                # 编号单独一段，别用 `(k or '') and …` 那种表达式（实测渲染出
                # 「【卡片1 】」，尾随空格会被模型当成正文的一部分）
                parts.append(f"【卡片{k}】{t}｜{d}" if k else f"【卡片】{t}｜{d}")

    _cards(slide.get("cards"))
    _bullets(slide.get("blocks"), "说明卡")
    for s in (slide.get("sections") or [])[:3]:
        if isinstance(s, dict) and str(s.get("t") or "").strip():
            chips = "、".join(str(x) for x in (s.get("chips") or [])[:5])
            parts.append(f"【分段】{str(s['t']).strip()}｜{str(s.get('d') or '').strip()}"
                         + (f"｜标签：{chips}" if chips else ""))
    _bullets(slide.get("badges"), "资质徽章")
    _bullets(slide.get("steps"), "步骤卡")
    _bullets(slide.get("tags"), "适用工艺标签")

    c = slide.get("contrast")
    if isinstance(c, dict):
        wrong = c.get("wrong") or c.get("left")
        right = c.get("right")
        if wrong or right:
            parts.append(f"【对比块】左灰底：{str(wrong or '').strip()}"
                         f"；右淡绿底打绿色✓：{str(right or '').strip()}")

    tail = str(slide.get("tail") or "").strip()
    if tail:
        parts.append(f"【底部总结句】{tail}")
    parts.append(footer)
    parts.append("画面中不得出现价格、电话、完整地址与二维码。")
    return "\n".join(parts)


def image_plan_for(cfg: dict, copy_text: str, city: str,
                   n: int) -> tuple[list[str], list[str]]:
    """产出本内容包要用的 N 条生图提示词 + 创意指令摘出的标签。

    返回 `(prompts, tags)`；标签为空时调用方沿用文案自带标签。规划失败 → 退回
    配置的兜底池（也要套风格底座 + 逐条渲染变量，见下）。
    """
    from app.services import douyin_vars

    v = douyin_vars.visual_vars(cfg, city, copy_text, n)
    # 页面上配置的画风（空 = 用内置 STYLE）
    style = str(cfg.get("style_prompt") or "").strip()
    # 海报底部行 = `{address}`（页面：「服务行内容 — 会同时用在文案末行和海报地址行」）
    footer = f"底部一行小字：{v['address']}" if v.get("address") else FOOTER
    plan = plan_pack(cfg, copy_text, city, n)
    slides = plan.get("slides") or []

    if slides:
        prompts = [render_prompt(s, footer, brand=v.get("brand") or "",
                                 style=style) for s in slides]
        while len(prompts) < n:                 # 图纲条数不足 → 复用最后一条的版式
            prompts.append(prompts[-1])
        return prompts[:n], list(plan.get("tags") or [])

    # 规划失败（LLM 挂了 / 返回不合规）：**已经没有"提示词兜底池"可退**，改用
    # 文案首句 + 用户配置的画风现拼一条通用科普版式，保证这一包仍有图可发。
    # 旧实现靠 `image_prompts` 池子补位，但池子只在失败时生效，用户改了也看不到
    # 效果（2026-11-10 按用户要求整个功能下线，换成 `style_prompt` 决定画风）。
    base = style or STYLE
    brand_line = _brand_label(v.get("brand") or "")
    head = (f"【顶部正中品牌名（特大号，字号加大）】{brand_line}\n"
            if brand_line else "")
    first = re.split(r"[。！？\n]", str(copy_text or "").strip())[0][:40].strip()
    fallback_text = first or f"{city}服务科普"
    # 版式按张轮转（封面/知识/清单/流程）：全用同一种的话，N 张图会长得一模一样
    roles = ("cover", "knowledge", "checklist", "flow")
    out = []
    for k in range(n):
        role = roles[k % len(roles)]
        out.append(f"{base}\n【版式】{LAYOUTS[role]}\n{head}【内容】{fallback_text}\n{footer}")
    return out, []


def image_prompts_for(cfg: dict, copy_text: str, city: str, n: int) -> list[str]:
    """只取提示词的兼容入口（既有调用方仍在用）。"""
    return image_plan_for(cfg, copy_text, city, n)[0]