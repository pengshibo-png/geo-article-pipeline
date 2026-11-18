"""模型产出 HTML 的白名单消毒（仅标准库，无第三方依赖）。

## 为什么必须做，且必须做在**写侧**（2026-10-17 定位）

`01_draft.html` / `03_final.html` 是**模型输出原样落盘**的。档案页预览把文件内容
当 HTML 渲染（`innerHTML`），于是存在一条真实的**存储型 XSS** 通路：

    提示词注入（或模型被网页/素材内容带的坏样本）→ 正文里出现 <script> /
    <img onerror> → 落盘 → **每次打开档案页都执行**

它比普通 XSS 更糟：脚本跑在同源页面里，能读 cookie、能带 CSRF 令牌调
「保存 AI 指令」「发布」这些接口 —— 一次注入 = 长期越权（改成别人的提示词，
每篇新文章都替你带私货）。

消毒放写侧而不是读侧的理由：落盘的旧文件读侧管不着，而**读侧只能当最后一道
兜底**（档案页预览另走 `sandbox=""` 的 iframe，见 client_archives.html 的
`renderArticle`）。写侧干净了，新产出就不会再带毒。

## 白名单口径与冻结规格一致

A 段要求"纯 HTML 片段（h2/h3/p/strong/table/ul/ol），**不含**
h1/html/head/body/link/script 标签"。实测 93 篇真实 01/03 文件里出现的标签恰好
是 p/td/strong/tr/h2/li/h3/th/br/table/b/ol/tbody/thead/ul，**零属性** ——
所以收紧到白名单不会损失任何既有产出（往返实测：93 篇文本逐字不变、体积不减）。

## 它是"重建"不是"黑名单删标签"

用 `HTMLParser` 解析后逐节点重建：允许的标签/属性按白名单重新拼，未列出的标签
丢标签留文字，可执行内容（script/style/iframe/svg…）连内容一起丢。黑名单做法
（正则删 `<script>`）挡不住 `<img onerror=`、`java\tscript:`、`<svg><script>`、
`style="width:expression()"` 这些变体，实测逐条都能绕过。
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

_ALLOWED_TAGS = frozenset({
    "p", "br", "hr", "h2", "h3", "h4", "strong", "b", "em", "i", "u", "s",
    "small", "sub", "sup", "span", "div", "blockquote", "pre", "code", "mark",
    "cite", "abbr", "time", "address", "section", "article", "figure",
    "figcaption", "table", "caption", "colgroup", "col", "thead", "tbody",
    "tfoot", "tr", "th", "td", "ul", "ol", "li", "dl", "dt", "dd", "img", "a",
})
# 标签连同**内容**一起丢（不是只丢标签）——否则 <script> 里的代码会变成
# 页面上的可见正文，既难看又可能被下一轮审稿当成文章内容。
_DROP_WITH_CONTENT = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "embed",
    "noscript", "template", "svg", "math", "canvas", "head", "title", "base",
})
_VOID_TAGS = frozenset({
    "br", "hr", "img", "col", "input", "meta", "link", "area", "base",
    "source", "wbr", "param", "track",
})
# 属性白名单：**不含 id**（DOM clobbering：id="view-body" 这类能顶掉预览页自己的
# 元素引用）、**不含 data-\***（既有产出零属性，没有保留的理由）。
_ALLOWED_ATTRS = frozenset({
    "class", "title", "alt", "src", "href", "width", "height", "colspan",
    "rowspan", "align", "valign", "target", "rel", "loading", "style",
})
_STYLE_PROPS = frozenset({
    "max-width", "min-width", "width", "max-height", "height", "text-align",
    "vertical-align", "font-weight", "font-style", "color", "background-color",
    "border", "border-collapse", "border-width", "border-style", "border-color",
    "padding", "margin", "line-height", "white-space",
})
# 值里一旦出现这些就整条 style 丢掉：url()/expression/反斜杠转义是 CSS 侧
# 执行/外链的入口，@ 与 /* */ 用来打断解析。
_STYLE_VALUE_BAD = re.compile(r"url\s*\(|expression|javascript|@|\\|/\*", re.I)
_STYLE_VALUE_OK = re.compile(r"^[#%.,()\-\w\s]+$")
# 危险协议：先去掉空白与 C0 控制字符再判，否则 `java\tscript:` / `java\nscript:`
# 能绕过朴素的 startswith 检查（浏览器会忽略其中的空白）。
_BAD_SCHEME = re.compile(r"^(?:javascript|vbscript|data|file|blob)\s*:", re.I)
_CTRL_WS = re.compile(r"[\x00-\x20]+")


def _safe_url(value: str) -> bool:
    return not _BAD_SCHEME.match(_CTRL_WS.sub("", value or ""))


def _safe_style(value: str) -> str | None:
    """style 消毒：逐条声明检查，只保留白名单属性 + 干净值；全废则丢属性。"""
    keep: list[str] = []
    for decl in (value or "").split(";"):
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop, val = prop.strip().lower(), val.strip()
        if prop not in _STYLE_PROPS or not val:
            continue
        if _STYLE_VALUE_BAD.search(val) or not _STYLE_VALUE_OK.match(val):
            continue
        keep.append(f"{prop}: {val}")
    return "; ".join(keep) or None


class _Sanitizer(HTMLParser):
    """白名单重建 HTML：保留允许的标签/属性，其余丢标签留文字，可执行内容全丢。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0            # >0 表示正处在"连内容一起丢"的标签里

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _DROP_WITH_CONTENT:
            self._skip += 1
            return
        if self._skip or tag not in _ALLOWED_TAGS:
            return
        self.out.append(f"<{tag}{self._clean_attrs(attrs)}>")

    def handle_startendtag(self, tag, attrs) -> None:
        """`<img/>`、`<br/>` 这类自闭合：当开标签用（void 不再补闭合）。

        自闭合的 `<script/>` **不能**推 skip 计数 —— 它没有内容段去配平，
        推了就再也不会递减，后面整篇正文被静默丢掉。
        """
        if tag in _DROP_WITH_CONTENT:
            return
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag) -> None:
        if tag in _DROP_WITH_CONTENT:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip or tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data) -> None:
        if not self._skip and data:
            self.out.append(escape(data, quote=False))

    def _clean_attrs(self, attrs) -> str:
        parts: list[str] = []
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if name not in _ALLOWED_ATTRS:      # 顺带挡住全部 on* 事件属性
                continue
            if name == "style":
                value = _safe_style(value) or ""
                if not value:
                    continue
            elif name in ("href", "src"):
                if not _safe_url(value):
                    continue
            elif name == "target" and value != "_blank":
                continue
            parts.append(f' {name}="{escape(value, quote=True)}"')
        if any(p.startswith(' target="_blank"') for p in parts) and not any(
                p.startswith(" rel=") for p in parts):
            parts.append(' rel="noopener noreferrer"')   # 防 tabnabbing
        return "".join(parts)


def sanitize_html(text: str) -> str:
    """把模型产出的 HTML 片段按白名单重建（安全兜底，见模块 docstring）。

    **失败即抛**（fail-closed）：消毒是安全控制，解析异常时返回原文等于把
    XSS 原样放行；宁可让这次生成失败（外层有格式重试与降级交付兜底）。
    """
    from app.errors import ValidationError

    s = _Sanitizer()
    try:
        s.feed(text or "")
        s.close()
    except Exception as exc:  # noqa: BLE001 - 解析器异常一律不放行原文
        raise ValidationError(f"HTML 消毒失败（{type(exc).__name__}）：拒绝写入未消毒正文") from exc
    return "".join(s.out).strip()