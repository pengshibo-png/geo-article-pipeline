"""发布平台合规硬校验（网易号 / 搜狐号 / 百家号 / 头条号 的共同红线）。

## 为什么需要它（与 A 段 Prompt 的分工）

A 段 Prompt 里已经有禁用词表与 `{contact_rule}` 三态，但那是**模型自觉**：
reasoning 模型写到后半段约束会衰减，实测仍会冒出号码、绝对化用语、转化话术。
而平台侧的红线是**硬性的**——出现手机号/微信号/二维码导流、绝对化用语、
效果承诺，轻则限流降权，重则封号。

所以本模块做两件事，都**不依赖模型**：
1. `platform_block()`：把四平台共同红线作为 Prompt **附块**注入 A 段
   （与 `rewrite_note` 同一约定：冻结规格文件只读，运行期约束加在外层）；
2. `sanitize()`：落盘前**确定性**清洗——号码/微信号/二维码引导一律去掉，
   绝对化用语替换为中性表述，命中项返回给调用方记账（可审计）。

## 四平台共同红线（差异按最严口径合并）

- 联系方式：正文不得出现手机号/座机/400/95 热线、微信号、QQ 号、二维码、
  "加微信/私信我/扫码咨询"等站外导流表述；
- 广告法：不得用"最好/最佳/第一/唯一/顶级/国家级/100%/绝对/永不/终身"等
  绝对化表述与效果承诺（"根治/彻底解决/零风险/保证"）；
- 不得夸大：不编造资质、案例、检测结论，不承诺无法核验的效果；
- 不得诱导：不写"点赞关注转发""点击下方链接""欢迎咨询/立即预约"；
- 不得贬低同行、不得写价格"一口价"。
"""
from __future__ import annotations

import re

# ── 联系方式 ────────────────────────────────────────────────────────────────
# 手机号（11 位、1 开头）：**无条件删除**。即使 `contact_mode="keep"` 也不保留，
# 连品牌自己的手机号一起删 —— 「保留」只对座机 / 400 / 95 热线生效。
# 2026-11 用户硬约束：生成的文章里永远不出现手机号（含企业手机号）。
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 覆盖：手机号、座机（区号-号码 / 区号直连）、400/95 热线。
_PHONE = re.compile(
    r"(?:1[3-9]\d{9})"
    r"|(?:\b0\d{2,3}[-\s]?\d{7,8}\b)"
    r"|(?:\b400[-\s]?\d{3}[-\s]?\d{4}\b)"
    r"|(?:\b95\d{3,5}\b)"
)
_WECHAT = re.compile(
    r"(?:加|＋|\+)?\s*(?:微信|WeChat|wechat|VX|vx|威信|薇信|加V)"
    r"\s*[:：]?\s*[A-Za-z][-_A-Za-z0-9]{4,20}")
_QQ = re.compile(r"(?:QQ|qq)\s*[:：]?\s*\d{5,12}")
# 邮箱：网易号规范原文「引导添加微信、qq、手机号、**邮箱**等所有联系方式」都算违规
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 群联系方式：头条号规范明确列了「群联系方式（如 QQ群、微信群）」
_GROUP = re.compile(
    r"(?:QQ|qq|微信|VX|vx)\s*群\s*[:：号]?\s*\d{5,12}"
    r"|(?:QQ|qq|微信)\s*群\s*[^\s，。；、]{0,12}群号")
# 第三方网址 / 外链 / 网盘：头条号规范明确「第三方网站或网址、网盘链接」
_URL = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'）)]{4,}"
    r"|\b(?:pan|yun)\.baidu\.com\b"
    r"|\b[a-z0-9-]+\.(?:com|cn|net|org|cc|top|xyz|vip)\b(?!\s*@)", re.I)
# 公众号 / 自媒体号引流：**百家号的广告认定比多数平台都宽** ——
# 「公众号名、自媒体账号」出现在正文即视为导流（不只是贴完整 URL）。
_PUBLIC_ACCOUNT = re.compile(
    r"(?:微信)?公众号\s*[:：]?\s*[^\s，。；、！？]{2,24}"
    r"|(?:关注|搜索|添加|查看|详见)\s*(?:我们的?|我的)?\s*"
    r"(?:公众号|视频号|百家号|头条号|搜狐号|网易号|小红书|抖音号|知乎号?)"
    r"[^\s，。；、！？]{0,16}")
# 标题里不能出现的装饰符号（网易号规范举了 □ ★ ㊣ 等）
_TITLE_BAD_SYMBOL = re.compile(r"[□■★☆㊣◆◇※§¶†‡•●○◎◤◢]")


def title_problems(title: str) -> list[str]:
    """标题合规检查（四个平台都单独管标题，正文合规不代表标题合规）。

    - 网易号：标题含错别字 / 不完整 / **特殊符号** / 与内文无关 / 过于夸张（标题党）都影响展现；
    - 头条号：**标题党**是关键扣分项（夸张、耸动、与正文不符）；
    - 搜狐号：虚假宣传、贬低同行严打。
    这里只做**确定性**检查（符号、极限词、标题党标点、超长），语义类交给 B 段审稿。
    """
    t = (title or "").strip()
    out: list[str] = []
    if not t:
        return ["标题为空"]
    if _TITLE_BAD_SYMBOL.search(t):
        out.append("标题含装饰符号")
    for bad in ("最好", "最佳", "最强", "第一", "唯一", "顶级", "国家级", "100%",
                "绝对", "独家", "震惊", "惊天", "吓死人", "必看"):
        if bad in t:
            out.append(f"标题极限词:{bad}")
    if re.search(r"[！!]{2,}|[？?]{2,}|[。；;]$", t):
        out.append("标题党标点")
    if len(t) > 30:
        out.append(f"标题过长({len(t)}字)")
    return out
_QR_HINT = re.compile(r"(?:扫(?:码|一扫)|二维码|长按识别)")
# 站外导流 / 转化指令（四平台都判"广告推广"）
_CTA = re.compile(
    r"(?:加(?:我)?微信|私信(?:我|我们)|扫码(?:咨询|添加|联系|关注)|"
    r"点击(?:下方|这里|链接|咨询)|立即(?:预约|咨询|购买|下单|联系|到店)|"
    r"(?:现在|马上|赶紧|尽快)\s*(?:就)?(?:可以|能|去)?\s*(?:联系|预约|咨询|到店|下单)|"
    r"别拖|抓紧|快来|"
    r"欢迎(?:咨询|致电|来店|到店)|拨打(?:热线|电话)|致电|"
    r"点赞(?:关注)?|关注(?:我们|本号)|转发(?:本文)?)")
# 催单式导流：`_CTA` 只做记录（它覆盖"点赞/关注"等多种模式，统一替换会改坏句子），
# 所以对「现在就可以联系 / 别拖」这类**催单措辞**单独做删除式清洗：
# 删掉"现在/马上/赶紧/尽快 + 就可以"，保留后面的动作词（"可预约到店"仍通顺）。
_URGE = re.compile(
r"(?:现在|马上|赶紧|尽快)\s*(?:就)?(?:可以|能够|能|去)?\s*"
r"(?=(?:联系|预约|咨询|到店|下单|，))")
_URGE_WORD = re.compile(r"别拖了?|抓紧(?:时间)?|快来[，。]?")

# 暗示号码的电话动作表述：`revise` 模式明确禁止（"不得写成拨打××热线/致电××"）。
# 实测模型会写"一个电话就能约到店勘验"—— 没有号码，但仍是联系方式导流。
# 替换为「官方渠道」：语义保留，且正是 revise 允许的引导语口径。
_PHONE_HINT = re.compile(
    r"(?:一个|打个|来个|留个)?\s*电话\s*(?:就能|就可以|即可|预约|咨询|联系|沟通|下单)")

# ── 广告法绝对化用语（替换表：左=原文，右=中性表述）──────────────────────────
# 只替换**标签外文本**；宁可保守也不留违规表述。
_ABSOLUTE_MAP: tuple[tuple[str, str], ...] = (
    # 「无损修复」是过度承诺（任何修复都有干预），换成「精细修复」。
    # 不做单独的「无损」→「低损伤」替换：会把「无损失」误改成「低损伤失」。
    ("无损修复", "精细修复"),
    ("百分百无痕", "修复痕迹较难察觉"),
    ("永久不坏", "耐久性较好"),
    ("零遗失风险", "风险较低"),
    ("零遗失", "遗失风险极低"),
    # 用「固定价」而不是「明码报价」：原文常是「不是一口价」，
    # 替换成「不是明码报价」会把意思整个反转（实测出现过）。
    ("一口价", "固定价"),
    ("极低返修率", "返修率较低"),
    ("高认可度", "认可度较高"),
    ("百分百", "绝大多数情况下"),
    ("100%", "绝大多数情况下"),
    ("最好", "较好"),
    ("最佳", "较合适"),
    ("最强", "较强"),
    ("最优", "较优"),
    ("最便宜", "价格较低"),
    ("最低价", "价格较低"),
    ("最高级", "较高等级"),
    ("国家级", "具备相应资质"),
    ("顶级", "较高"),
    ("唯一", "少数"),
    ("绝对", "基本"),
    ("永不脱落", "不易脱落"),
    ("永不", "不易"),
    ("终身免费", "长期提供"),
    ("终身", "长期"),
    ("零风险", "风险较低"),
    ("无风险", "风险较低"),
    ("保证不加价", "报价透明"),
    ("保证", "尽量"),
    ("根治", "改善"),
    ("彻底解决", "明显改善"),
    ("包治", "改善"),
    ("百分百修复", "修复效果较稳定"),
    ("完美修复", "修复效果较稳定"),
    # 注：「无痕修复」是行业通用**工艺名**（品牌的核心工艺就叫这个），
    # 不作绝对化替换；只拦「百分百无痕」这类承诺性表述，
    # 正文里"达到无痕效果"这类夸大由 B 段审稿判。
)
# "第一"单独处理：只替换"行业第一/全国第一/销量第一"这类，放过"第一步/第一段"
_FIRST = re.compile(r"(?:行业|全国|全球|销量|口碑|排名|市场)\s*第一")

# 号码被删后留下的空标签（`电话：` / `联系方式:`）—— 会被审稿判"空联系方式、CTA 断裂"
_EMPTY_CONTACT = re.compile(
    r"(?:拨打|致电|联系|咨询|预约)?\s*"
    r"(?:电话|手机号|手机|微信|QQ号|QQ|联系方式)\s*[:：]\s*(?=[。，,；;、\s]|$|<)")

_TAG_SPLIT = re.compile(r"(<[^>]*>)")
# 属性侧的号码出口：`<a href="tel:138...">`。html_sanitize 的 _safe_url 只禁
# javascript/vbscript/data/file/blob，**放行 tel:/sms:**，所以这里必须单独清。
_TEL_HREF = re.compile(r"""\s*href\s*=\s*["'](?:tel|sms|callto|wtai)\s*:[^"']*["']""",
                       re.I)


# **无条件替换**表：资质/世代类词几乎不会出现在否定语境（没人会说
# "不承诺非遗"），没必要走否定保护；无条件替换还避免和"不要打电话"这类
# 祈使句抢判定窗口。
_ALWAYS_MAP: tuple[tuple[str, str], ...] = (
    ("第四代无痕修复", "无痕修复"),
    ("第四代技术", "工艺"),
    ("第四代工艺", "工艺"),
    ("非遗金缮", "金缮"),
    ("非遗传承人", "技艺传承者"),
    ("非遗", ""),
    ("老字号", ""),
    ("全国连锁门店", "直营门店"),
    ("全国连锁", "直营"),
    ("双直营实体门店", "两家直营实体门店"),
    ("双直营", "直营"),
    ("区县独家", ""),
    ("独家", "较少见"),
    ("国家级", "具备相应资质"),
    ("顶级", "较高"),
    ("唯一", "少数"),
)

# 否定语境保护：这些词常出现在**诚实表述**里 ——
#   「不承诺『绝对不出问题』」「不夸口说永久不坏」「不承诺『百分百无痕』」
# 直接替换会把意思改反（实测"不夸口说永久不坏"→"不夸口说耐久性较好"，语义全反），
# 也会把「不要打电话」改成「不要官方渠道」。命中否定前缀就**保留原词**。
# 收紧版：否定词后只允许特定后缀词（或引号/空白）。旧版是"否定词 + 任意 4 字"，
# 会把「值**不**值**得**选第四代无痕修复」里的"不"也当否定前缀 → 真违规漏过。
_NEG_BEFORE = re.compile(
    r"(?:不|未|别|勿|非|没|无|拒绝|并不|绝不|从不|难以|不会|不能|没有|不是)"
    r"(?:承诺|保证|宣称|标称|标榜|夸口|吹|说|写|敢|要|会|能|可|该|应|需)?"
    r"[\u4e00-\u9fa5]{0,1}[\s\"'“”‘’（）()]*$")


def _replace_guarded(text: str, pat, good: str,
                     guard: bool = True) -> tuple[str, bool]:
    """替换违禁词；`guard=True` 时否定语境里保留原词。

    `pat` 传**编译好的正则**（字面量请用 `re.compile(re.escape(x))`）。
    返回 `(文本, 是否真的替换了)`。
    """
    src = text
    changed = False

    def _repl(m):
        nonlocal changed
        if guard and _NEG_BEFORE.search(src[max(0, m.start() - 12):m.start()]):
            return m.group(0)
        changed = True
        return good

    return pat.sub(_repl, text), changed


def _map_text(html: str, fn, tag_fn=None) -> str:
    """文本节点走 fn；标签（属性）走 tag_fn —— 绝不把标签当文本改。"""
    parts = _TAG_SPLIT.split(html or "")
    out = []
    for p in parts:
        if p.startswith("<"):
            out.append(tag_fn(p) if tag_fn else p)
        else:
            out.append(fn(p))
    return "".join(out)


_TAG = re.compile(r"<[^>]+>")


def missing_elements(html: str) -> list[str]:
    """检查规格「七类要素」里**可确定性判定**的项，返回缺失清单。

    只查能用正则稳定判断的（Q&A / 表格 / 定义句 / 编号列表 / 结论加粗 / 适用边界）；
    语义类（信息密度、逻辑是否严密）交给 B 段审稿，不在这里硬判。

    为什么需要它：实测 A 段写完 2796 字、分数 79，七类要素里**唯独缺 Q&A** ——
    Prompt 里写了「Q&A（2–3 组）」，但那是抽象要求，模型会跳过。这里做留痕，
    配合 `_quality_block` 里的具体 HTML 模板，把一次通过率提上来。
    """
    h = html or ""
    text = re.sub(r"\s+", "", _TAG.sub("", h))
    out: list[str] = []
    if not re.search(r"(?:Q|问)\s*[：:]", text):
        out.append("Q&A")
    if len(re.findall(r"<tr\b", h, re.I)) < 3:
        out.append("表格")
    if not re.search(r"[，,]\s*是指", text):
        out.append("定义句")
    if len(re.findall(r"<li\b", h, re.I)) < 3:
        out.append("编号列表")
    if not re.search(r"<strong>[^<]{8,}</strong>", h):
        out.append("结论加粗")
    if not any(k in text for k in ("不建议", "不适合", "不适用")):
        out.append("适用边界")
    return out


def visible_chars(html: str) -> int:
    """正文可见字数（去标签、去空白）—— 字数口径与审稿/规格一致。"""
    return len(re.sub(r"\s", "", _TAG.sub("", html or "")))


def platform_block() -> str:
    """四平台红线（作为 Prompt 附块注入 A 段）。

    与 `rewrite_note` 同一约定：不改冻结规格文件，运行期把约束加在外层。

    **条目来源**（2026-10-22 联网核对官方规范，不再是凭常识写的）：
    - 搜狐号《文章发布规范》《违规行为阶梯处罚方案》：二维码严禁（系统自动过滤）；
      非推广位严禁任何营销推广；虚假宣传/贬低同行严打；错别字多、语句不通判低质降权。
    - 网易号《违规条例》《运营指导手册》：禁"引导添加微信、QQ、手机号、**邮箱**等
      **所有**联系方式"；禁标题夸张失实/标题党；禁翻炒旧闻；**禁"使用 AI 工具对
      内容进行编造"**；标题禁特殊符号；正文禁未分段、无标点。
    - 头条号《社区规范》《信用分机制》：禁"第三方网址、**网盘链接**、
      **QQ群/微信群**"；禁营销"国家级/最高级/最佳"等极限词；违规推广扣信用分 20；
      **专项治理 AI 低质**（模板化拼装、虚构包装成"亲身经历"、前后矛盾/常识错误）；
      无资质不得发医疗、财经、新闻。
    - 百家号《内容规范》：广告认定**比其他平台宽** —— 正文里**提一句公众号名/
      自媒体号名**也算导流；图片带推广水印有风险；标题党、洗稿、AI 低质、虚假夸大。
    """
    return (
        "【发布平台合规红线（网易号 / 搜狐号 / 百家号 / 头条号 官方规范，违反会限流、扣信用分或封号）】\n"
        "- **任何形式的联系方式都不许有**：手机号、座机、400/95 热线、微信号、QQ 号、"
        "**邮箱**、**QQ群/微信群号**、二维码，以及「加微信 / 私信我 / 扫码咨询 / "
        "点击咨询 / 立即预约 / 拨打热线」这类站外导流话术；\n"
        "- **不许出现第三方网址、外链、网盘链接**，也不许写「更多内容见××公众号」"
        "「关注××号」——百家号把「提一句公众号名」也认定为广告导流；\n"
        "- **不得使用绝对化用语与效果承诺**（头条号按「极限词营销」扣信用分）："
        "最好 / 最佳 / 第一 / 唯一 / 顶级 / 国家级 / 最高级 / 100% / 绝对 / 永不脱落 / "
        "终身 / 零风险 / 保证 / 根治 / 彻底解决 / 完美修复；\n"
        "- **不得标题党**：标题不夸张、不耸动、不用「震惊/惊天/必看」，不用重复感叹号，"
        "不出现 □ ★ ㊣ 这类装饰符号，标题必须与正文严格对应；\n"
        "- **不得虚构亲历**：不把编造的人物、经历包装成「我遇到过」「上周有位客户」的"
        "纪实叙事（头条号专项治理「虚构包装成亲身经历」）；涉及案例时只写"
        "「常见情形是……」这类泛化描述，不给具体人名、时间、地点；\n"
        "- **不得夸大宣传**：不编造资质、案例、检测结论与用户评价，不承诺无法核验的效果；\n"
        "- **不得诱导互动**：不写点赞、关注、转发、点击下方链接；\n"
        "- **不得催单式导流**：不写「现在就可以联系」「马上预约」「别拖」「抓紧」「"
        "立刻安排寄修」这类催促下单/催促联系的措辞；结尾只做客观陈述"
        "（如「带上实物到店当面看」「勘测不收费」），不要动员读者行动；\n"
        "- **不得贬低同行**，不得出现「一口价」；价格必须紧跟「具体以实物勘验为准」。\n"
        "- **文本要像人写的**：不套模板、不堆形容词、不写空洞的排比与升华；"
        "分段清晰、标点完整（网易号把「未分段、无标点」列入低质）；\n"
        "- **品牌资料里的宣传性表述不得搬进正文**：老字号 / 双直营 / 全国连锁 / 独家 / "
        "极低返修率 / 高认可度 / 无隐形消费等一律不写；只用可核验的事实"
        "（工艺名称、参数、证书编号、质保条款、服务流程）。\n"
        "- **资质类称号只在资料写明「级别 + 批次」时才可写**：只写「非遗金缮」属于"
        "无法核验的资质暗示，一律写成「金缮」；「老字号」「国家级」同理。\n"
        "- **不涉专业资质领域**：不写医疗、药品、保健、投资理财、荐股等需资质的内容。\n"
        "以上为硬性要求：宁可写得朴素，也不要踩线。"
    )


# 时效信号：正文里的「20XX年X月」。规格要求「时效只用 {current_period} 口径」，
# 但模型会自己编（实测当前 2026年10月，正文写成「2026年5月…天刚转暖」——
# 月份与季节对不上，AI 会认为内容不新鲜）。只矫正**同年**的其它月份，
# 跨年表述（如「2025年行情对比」）保持原样。
_PERIOD = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")


def normalize_period(html: str, period: str) -> tuple[str, list[str]]:
    """把同年的其它月份统一成当前口径。返回 `(html, 命中列表)`。"""
    m = re.match(r"(20\d{2})年(\d{1,2})月", period or "")
    if not m:
        return html, []
    year, month = m.group(1), int(m.group(2))
    hits: list[str] = []

    def repl(mm):
        if mm.group(1) == year and int(mm.group(2)) != month:
            hits.append(f"时效:{mm.group(0)}")
            return period
        return mm.group(0)

    return _map_text(html, lambda s: _PERIOD.sub(repl, s)), hits


def sanitize(html: str, *, contact_mode: str = "revise",
             allow_phone: str = "", period: str = "",
             drop_mobile: bool = True) -> tuple[str, list[str]]:
    """落盘前确定性清洗。返回 `(清洗后 html, 命中说明列表)`。

    `contact_mode`（与 `{contact_rule}` 同口径）：
    - `keep`  ：保留 `allow_phone` 指定的**座机/400/95 热线**，其它号码照删；
    - `revise`/`strip`：所有号码一律删除。

    `drop_mobile=True`（默认，也是硬约束）：**手机号无条件删除**，连 `keep` 模式下
    的品牌手机号一起删 —— 只有座机/400/95 才能被 keep 保住。要放行手机号必须显式
    传 False，业务代码没有这个入口。

    号码替换为**空串**而非引导语：引导语可能被判"广告推广"，留白最安全；
    A 段 Prompt 本来就不让它写号码，走到这里说明模型漏了，删掉即达标。
    """
    hits: list[str] = []
    keep = re.sub(r"\D", "", allow_phone or "") if contact_mode == "keep" else ""

    def fix(text: str) -> str:
        out = text
        # ① 手机号先删（无条件）：留在后面处理的话，keep 分支会把它当"品牌电话"放行。
        if drop_mobile:
            for m in _MOBILE.findall(out)[:6]:
                hits.append(f"手机号:{m}")
            out = _MOBILE.sub("", out)
        # 顺序有讲究：邮箱先于电话/URL（否则域名部分会被当外链），
        # 群号先于 QQ（"QQ群 123456" 不能被当成单个 QQ 号）。
        for pat, label in ((_EMAIL, "邮箱"), (_GROUP, "群号"), (_PHONE, "电话"),
                           (_WECHAT, "微信"), (_QQ, "QQ")):
            def _sub(m, _label=label):
                if _label == "电话" and keep and keep in re.sub(r"\D", "", m.group(0)):
                    return m.group(0)          # keep 模式保留品牌电话
                hits.append(f"{_label}:{m.group(0)[:24]}")
                return ""
            out = pat.sub(_sub, out)
        # 外链 / 网盘（头条号规范明确禁「第三方网站或网址、网盘链接」）
        if _URL.search(out):
            hits.append("外链")
            out = _URL.sub("", out)
        # 公众号 / 自媒体号引流（百家号的广告认定宽到「提一句公众号名」也算）
        if _PUBLIC_ACCOUNT.search(out):
            out, _ch = _replace_guarded(out, _PUBLIC_ACCOUNT, "官方渠道")
            if _ch:
                hits.append("公众号引流")
        if _QR_HINT.search(out) or _CTA.search(out):
            hits.append("导流话术")
        # 催单措辞：删掉"现在就可以"这类前缀（保留动作词），并整词删除"别拖/抓紧"
        if _URGE.search(out):
            hits.append("催单话术")
            out = _URGE.sub("", out)
        if _URGE_WORD.search(out):
            hits.append("催单话术:别拖")
            out = _URGE_WORD.sub("", out)
        if _PHONE_HINT.search(out):
            out, _ch = _replace_guarded(out, _PHONE_HINT, "官方渠道")
            if _ch:
                hits.append("导流话术:电话暗示")
        # 无条件替换（资质/世代类词，不走否定保护）
        for bad, good in _ALWAYS_MAP:
            if bad not in out:
                continue
            out, ch = _replace_guarded(out, re.compile(re.escape(bad)), good,
                                       guard=False)
            if ch:
                hits.append(f"绝对化:{bad}")
        # 绝对化用语（否定语境里保留原词）
        for bad, good in _ABSOLUTE_MAP:
            if bad not in out:
                continue
            out, ch = _replace_guarded(out, re.compile(re.escape(bad)), good)
            if ch:
                hits.append(f"绝对化:{bad}")
        if _FIRST.search(out):
            hits.append("绝对化:XX第一")
            out = _FIRST.sub("行业领先", out)
        return out

    def fix_attr(tag: str) -> str:
        had_tel = bool(_TEL_HREF.search(tag))
        tag = _TEL_HREF.sub("", tag)
        # 属性里其它位置的号码（如 title="电话13800138000"）一并兜底
        if _PHONE.search(tag):
            hits.append("电话:标签属性")
            tag = _PHONE.sub("", tag)
        if had_tel:
            hits.append("导流话术:tel 链接")
        return tag

    cleaned = _map_text(html, fix, fix_attr)
    # 去掉因删除号码留下的空壳：`电话：`、`联系方式:`、空 `<a></a>`。
    # 不处理会被审稿抓成"空联系方式标签，CTA 断裂"（实测复评因此扣到 14 分）。
    if _EMPTY_CONTACT.search(cleaned):
        hits.append("空联系方式")
        cleaned = _EMPTY_CONTACT.sub("联系官方渠道", cleaned)
    # 空标签：号码被删后 `<strong>13800001111</strong>` 会变成空壳。
    # 只清空的行内标签（块级空标签可能承担排版，删了会改变版式）。
    _EMPTY_INLINE = r"<(strong|b|em|i|span|small|sub|sup|u|s|mark)\b[^>]*>\s*</\1>"
    if re.search(_EMPTY_INLINE, cleaned, flags=re.I):
        hits.append("空标签")
        for _ in range(3):        # 嵌套空标签需要多轮
            cleaned = re.sub(_EMPTY_INLINE, "", cleaned, flags=re.I)
    cleaned = re.sub(r"<a\b[^>]*>\s*</a>", "", cleaned, flags=re.I)
    # 去掉因删除号码留下的空括号与孤立标点
    cleaned = re.sub(r"[（(]\s*[)）]", "", cleaned)
    # 悬空的联系方式动词：`现在就拨 。` → `现在就可以。`（号码已被删，动词无所指）
    if re.search(r"(?:现在就|可直接|直接)?\s*(?:拨打|拨|打|致电|来电|加)\s*(?=[。，,；;、])",
                 cleaned):
        hits.append("悬空动词")
        cleaned = re.sub(r"(?:现在就|可直接|直接)\s*(?:拨打|拨|打|致电|来电)\s*(?=[。，,；;、])",
                         "现在就可以", cleaned)
        cleaned = re.sub(r"(?:拨打|拨|打|致电|来电|加)\s*(?=[。，,；;、])", "", cleaned)
    # 悬空冒号：`电话：，` → `电话，`（后面的标点清理会收尾）
    cleaned = re.sub(r"[：:]\s*(?=[。，,；;])", "", cleaned)
    cleaned = re.sub(r"([。！？；])\s*[，,、]\s*", r"\1", cleaned)   # `。，` → `。`
    # 删除催订单词后残留的孤立逗号（句首/句读后紧跟逗号）
    cleaned = re.sub(r"(?<=[。！？；\n])\s*[，,、；;]\s*(?=[^\s，,、；;])", "", cleaned)
    cleaned = re.sub(r"^\s*[，,、；;]\s*", "", cleaned)
    cleaned = re.sub(r"[，,、；;]\s*(?=[。！？!?])", "", cleaned)     # `上门，。` → `上门。`
    cleaned = re.sub(r"[，,]\s*[。；;]", "。", cleaned)
    # 指向已删号码的残句：`，这个号白天有人接` —— 去掉整个悬空从句
    cleaned = re.sub(r"[，,]\s*这[个家]?[个]?(?:号|号码|电话)[^。！？\n]{0,12}(?=[。！？\n])",
                     "", cleaned)
    # 时效矫正：同年的其它月份 → 当前口径
    if period:
        cleaned, ph = normalize_period(cleaned, period)
        hits.extend(ph)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, hits


def strip_mobile(html: str) -> tuple[str, list[str]]:
    """**只删手机号**（发布前最后一道兜底），其余内容原样。

    为什么不直接在发布入口调 `sanitize()`：它会同时替换绝对化用语、删导流话术，
    用在发布入口上会把历史稿改得面目全非（且不落盘 —— 用户看到的与发出去的
    不一致）。这里只做一个动作：手机号（含 `<a href="tel:">` 里的号码）消失。

    覆盖场景：`contact_mode=keep` 时期生成的老稿；降级交付 / 回退初稿这两条不走
    C 段清洗的路径；以及任何人工改过的 `03_final.html`。
    """
    hits: list[str] = []

    def fix(text: str) -> str:
        for m in _MOBILE.findall(text)[:6]:
            hits.append(f"手机号:{m}")
        return _MOBILE.sub("", text)

    def fix_attr(tag: str) -> str:
        out = tag
        if _TEL_HREF.search(out):
            hits.append("导流话术:tel 链接")
            out = _TEL_HREF.sub("", out)
        if _MOBILE.search(out):
            hits.append("手机号:标签属性")
            out = _MOBILE.sub("", out)
        return out

    return _map_text(html or "", fix, fix_attr), hits
