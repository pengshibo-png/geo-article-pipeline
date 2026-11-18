"""真模型输出的格式清洗（只去包装，不动正文，不放松冻结口径）。

覆盖 T/A/C 三段的真模型常见病：
  - Markdown 围栏：```html … ``` （未闭合也剥）
  - 首尾寒暄散文："好的，以下是文章：…希望符合要求。"
  - 标题行编号/加粗/引号包装："1. **标题**"

B 段 JSON 的兼容（维度名/verdict/score 类型/location 定位）在
`review_compat.py`，那层更重；本模块只管文本包装。

另外 `clean_html` 末尾挂了一道**白名单消毒**（实现在 `html_sanitize.py`）：
模型输出会被原样写进 01/03_*.html，而档案页是把它当 HTML 渲染的，不消毒就是
**存储型 XSS**（提示词注入 → 脚本进正文 → 每次打开档案都执行）。理由与口径
写在该模块 docstring 里，这里只调用。

设计红线：
  - 无任何 HTML 标签时**不删空**——模型输出事故要留证据给审稿判，
    清洗层擅自删空会把错因一起删掉；
  - "1.5 万预算"这类小数开头标题不能被编号正则误伤（ASCII 点后须有空格）。
"""
from __future__ import annotations

import re

from app.services.writer.html_sanitize import sanitize_html

_FENCE_OPEN = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\r?\n?")
_FENCE_CLOSE = re.compile(r"\r?\n?```[ \t]*$")
_TAG = re.compile(r"<[a-zA-Z/][^<>]*>")
_NUM_PREFIX = re.compile(
    r"^\s*(?:"
    r"\d{1,3}\s*[、．,，]"                 # 1、 / 1， / 1．
    r"|\d{1,3}\.\s+"                      # "1. "（点后须空格：不误伤 1.5 万）
    r"|[(（]\s*\d{1,3}\s*[)）]\s*"         # （1）/ (1)
    r"|[一二三四五六七八九十]{1,3}\s*[、．]\s*"   # 一、
    r")"
)
_LIST_MARK = re.compile(r"^[-+•·]\s+")     # 列表符号（后须有空格，防误伤连字符标题）


def strip_fences(text: str) -> str:
    """剥离开头/结尾的 Markdown 围栏（可多层；未闭合只剥开的那个）。"""
    t = (text or "").strip()
    while _FENCE_OPEN.search(t):
        t = _FENCE_OPEN.sub("", t, count=1)
        if _FENCE_CLOSE.search(t):
            t = _FENCE_CLOSE.sub("", t, count=1)
        t = t.strip()
    return t


def strip_edge_prose(text: str) -> str:
    """删掉首个 HTML 标签前的散文与最后一个标签后的散文（标签间正文不动）。"""
    t = (text or "").strip()
    tags = list(_TAG.finditer(t))
    if not tags:
        return t                     # 无标签：原样返回，不删空
    return t[tags[0].start(): tags[-1].end()].strip()


def clean_html(text: str) -> str:
    """A/C 段正文清洗：围栏 + 首尾散文 + **白名单消毒**（消毒实现在
    `html_sanitize.py`，A3 行数预算拆出）。

    消毒放在最后一步：先按标签定位正文边界（strip_edge_prose 依赖 _TAG 正则），
    再重建剩下的部分。顺序颠倒的话，被去掉的属性/标签会让边界判断失真。
    """
    return sanitize_html(strip_edge_prose(strip_fences(text)))


# ── 分节文本（AI 洞察 / 结构化结论）─────────────────────────────────────────
_SEC_HEAD = re.compile(r"^\s*【[^】]{1,20}】")
_BULLET = re.compile(r"^\s*[-•·*＋]\s*")


def clean_sections(text: str) -> str:
    """分节文本清洗：剥围栏 → 丢掉首个小节前的寒暄/前言 → 只保留【小节】标题
    与其下的条目行。

    真模型输出洞察时常先来一句"好的，以下是分析："再给正文，且会用围栏包住
    整段；页面按【小节】切卡片渲染，混入前言会多出一个无标题卡片。
    """
    t = strip_fences(text or "")
    out: list[str] = []
    started = False
    for ln in t.splitlines():
        ln = ln.rstrip()
        if _SEC_HEAD.match(ln):
            started = True
        if not started:
            continue                      # 首个【小节】之前一律丢弃（前言/寒暄）
        if not ln.strip():
            out.append("")
            continue
        if _SEC_HEAD.match(ln) or _BULLET.match(ln):
            out.append(ln.strip())
    return "\n".join(out).strip()


def clean_title_line(line: str) -> str:
    """T 段标题行清洗：编号 / 加粗 / 引号包装（循环到稳定，只去包装字符）。"""
    t = (line or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _NUM_PREFIX.sub("", t)
        t = _LIST_MARK.sub("", t)
        t = t.strip().strip("*_`").strip().strip("\"'“”‘’").strip()
    return t


_Q_END = re.compile(r"[？?]")


def keep_question_only(line: str) -> str:
    """GEO 标题只保留问句本身：砍掉问号后面的"做法建议 / 卖点 / 行动指令"尾巴。

    实测（2026-10-17）：真模型顽固沿用"前半句问法 + 后半句钩子"的 SEO 句式，
    而且库里的历史标题会持续把新标题带偏。反复调 Prompt 收效有限，所以做
    **确定性截断**：问号之后的内容一律丢弃（问号保留）。

    例：
        上海翡翠手镯摔裂修复多少钱？先勘测再一口价  →  上海翡翠手镯摔裂修复多少钱？
        和田玉手镯有暗裂要不要修？别白花钱          →  和田玉手镯有暗裂要不要修？

    没有问号的陈述式标题原样返回（GEO 也接受这种问题索引）。
    """
    t = (line or "").strip()
    m = _Q_END.search(t)
    return t[: m.end()].strip() if m else t


# ── 定稿产出校验（C 段下游没有审稿兜底，必须自己把关）────────────────────────
_ARTICLE_TAG = re.compile(r"<(p|h2|h3|table|ul|ol|li|blockquote|strong|b)\b", re.I)
# 审稿 JSON 的字段名出现在正文**任意位置**都算泄漏。
# 旧校验只查"开头是 { 且含 overall_score"，而 clean_html 早把 JSON 前缀裁掉了，
# 判断因此永假 —— 实测漏过一篇（一句结论 + JSON 残片拿了 14 分）。
# 只认这一条**确定性特征**：不加字数/结构启发式阈值，那会误伤正常短稿。
_JSON_LEAK = re.compile(
    r'"(?:overall_score|dimension_scores|issues|verdict|highlights|severity)"\s*:')

HTML_ONLY_HINT = (
    "\n\n【上一次输出不是文章正文，请重做】\n"
    "- 只输出修改后的 HTML 正文（h2 / p / table / ul / ol / strong 等标签）；\n"
    '- 禁止输出审稿报告、JSON，或任何形如 {"overall_score": ...} 的内容；\n'
    "- 不要输出解释文字，不要代码块标记。")


def validate_article_html(text: str, stage: str = "C 段") -> str:
    """确认模型产出的是**文章 HTML**，否则抛错（交给重试/降级，绝不写进定稿）。

    为什么只在 C 段做：A 段产出会进 B 段审稿，错内容会被审稿判低分并触发重写，
    天然有兜底；而 C 段是**最后一道**，下游没有任何复核。

    实测事故（2026-10-17）：C 段有两次把【审稿报告 JSON】当正文返回，而代码只
    校验"非空"，于是 `03_final.html` 里装的是一段 `{"overall_score": ...}` ——
    页面看起来正常，实际品牌名 0 次、正文标签 0 个，完全不可用。
    """
    from app.errors import ValidationError

    body = (text or "").strip()
    if not body:
        raise ValidationError(f"{stage}返回空正文")
    if body.lstrip().startswith("{") and '"overall_score"' in body:
        raise ValidationError(f"{stage}把审稿报告当正文返回了（不是文章）")
    # JSON 字段出现在**任意位置**都算泄漏：clean_html 会把 JSON 前缀裁掉，
    # "开头是 {" 的判断因此永假（实测漏过一篇：一句结论 + JSON 残片拿了 14 分）。
    if _JSON_LEAK.search(body):
        raise ValidationError(f"{stage}正文里混入了审稿 JSON 字段（不是文章）")
    if not _ARTICLE_TAG.search(body):
        raise ValidationError(f"{stage}未产出 HTML 正文（疑似纯文本或 JSON）")
    return text
