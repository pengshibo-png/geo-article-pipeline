"""从品牌资料派生变量（唯一来源：S3 的 brand-names.md / contact-rule.md）。

**不复制规则正文** —— 规则文本从 S3 文件里**解析出来用**，而不是在代码里再写一份
（否则就长出了第二份定义，违反门禁 G10）。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.errors import ConfigError
from app.spec import brand_names_file, contact_rule_file

_FENCE = "```"

# 首句不得以代词/描述性短语开头（brand-names.md §4 的硬性要求）
_PRONOUN_PREFIX = (
    "我们", "我司", "本公司", "我店", "本店", "本工坊", "一家", "这", "那", "它", "他们", "你们",
)
# 地域词表（启发式去简称用）：直辖市 / 省会 / 主要地市 + 全国性前缀
_REGION_WORDS = frozenset({
    "上海", "北京", "天津", "重庆", "广州", "深圳", "杭州", "南京", "苏州", "成都", "武汉",
    "西安", "长沙", "青岛", "宁波", "温州", "无锡", "佛山", "东莞", "郑州", "合肥", "福州",
    "厦门", "昆明", "贵阳", "南昌", "济南", "沈阳", "大连", "哈尔滨", "长春", "石家庄", "太原",
    "兰州", "海口", "三亚", "珠海", "中山", "惠州", "嘉兴", "绍兴", "台州", "金华", "泉州",
    "徐州", "常州", "南通", "烟台", "潍坊", "唐山", "洛阳", "南宁", "银川", "西宁", "拉萨",
    "呼和浩特", "乌鲁木齐", "中国", "华夏", "全国", "中华",
})
_REGION_SUFFIX_RE = re.compile(r"^[\u4e00-\u9fff]{2,4}?(?:省|市)")
_INDUSTRY_SUFFIX = ("珠宝", "科技", "工程", "服务", "有限公司", "有限责任公司")

PHONE_RE = re.compile(r"1[3-9]\d{9}|0\d{2,3}-?\d{7,8}")


def _strip_region(name: str) -> str:
    """去掉地域前缀。剥掉后剩余不足 2 字则不剥（避免把名字剥空）。"""
    m = _REGION_SUFFIX_RE.match(name)
    if m and len(name) - len(m.group(0)) >= 2:
        return name[len(m.group(0)):]
    for word in sorted(_REGION_WORDS, key=len, reverse=True):
        if name.startswith(word) and len(name) - len(word) >= 2:
            return name[len(word):]
    return name


def derive_brand_names(content: str) -> dict[str, object]:
    """三步派生（对应 brand-names.md §2）：
    ① 主名 = 「品牌概述」首句第一个标点前  ② 别名-主体名 = 「资质与认证」的 主体名称：
    ③ 别名-简称 = 显式声明，否则启发式（标记待确认）
    """
    text = content or ""
    intro = text.split("【", 1)[0].strip()
    first = re.split(r"[，,。;；]", intro, maxsplit=1)[0].strip() if intro else ""
    # 首句以代词/描述性短语开头 → 视为无法识别主名（R2 阻塞）
    main = first if 0 < len(first) <= 20 and not first.startswith(_PRONOUN_PREFIX) else ""

    qual = ""
    m = re.search(r"【资质与认证】(.*?)(?=【|$)", text, re.S)
    if m:
        found = re.search(r"主体名称[:：]\s*([^\n]+)", m.group(1))
        qual = found.group(1).strip() if found else ""

    alias, alias_src = "", "-"
    decl = re.search(r"(?:简称|以下简称)[:：\"「']?\s*[「\"']?([^\"」'，,。；;\n]{2,10})", intro)
    if decl:
        alias, alias_src = decl.group(1).strip(), "declared"
    elif main:
        cand = re.sub("(" + "|".join(_INDUSTRY_SUFFIX) + ")$", "", _strip_region(main))
        if cand and cand != main:
            alias, alias_src = cand, "heuristic:待确认"

    return {"main": main, "entity": qual, "alias": alias, "alias_source": alias_src,
            "ok": bool(main), "consistent": (not qual) or qual == main}


def extract_phone(content: str) -> str:
    """{phone} 的唯一来源 = 品牌资料（A 段不再硬编码电话）。"""
    m = re.search(r"(?:咨询|预约|联系)?电话[:：]\s*(" + PHONE_RE.pattern + ")", content or "")
    if m:
        return m.group(1)
    found = PHONE_RE.search(content or "")
    return found.group(0) if found else ""


def normalize_contact(mode: str, phone: str) -> tuple[str, str]:
    """把 `(contact_mode, {phone})` 归一到「手机号永不入文」的口径。

    **2026-11 硬约束**：文章里永远不出现手机号（品牌自己的也不行）。所以品牌资料
    里那个号码若是手机号，`keep` 策略就不成立 —— 号码置空、策略降为 `revise`。

    为什么不在清洗层兜就算：Prompt 若按 keep 要求「电话至少出现 3 次」，而落盘前
    的确定性清洗必然把它删掉，正文会留下「可致电 。」这类残句。从源头不给号码，
    模型自然走「不写号码」的写法。
    """
    m = (mode or "revise").strip().lower()
    if m not in ("keep", "revise", "strip"):
        m = "revise"
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits.startswith("1"):
        return "revise", ""
    return m, str(phone or "").strip()


@lru_cache(maxsize=8)
def _contact_rule_blocks(path: str) -> dict[str, str]:
    """从 contact-rule.md 解析三种 mode 的渲染文本（唯一来源，不重写）。"""
    text = Path(path).read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    for mode in ("keep", "revise", "strip"):
        m = re.search(rf"###\s*mode\s*=\s*`{mode}`(.*?){_FENCE}(.*?){_FENCE}", text, re.S)
        if m:
            blocks[mode] = m.group(2).strip()
    if set(blocks) != {"keep", "revise", "strip"}:
        raise ConfigError(f"contact-rule.md 解析失败，仅取到 {sorted(blocks)}")
    return blocks


def render_contact_rule(mode: str = "revise", *, path: Path | None = None) -> str:
    mode = (mode or "revise").strip().lower()
    blocks = _contact_rule_blocks(str(path or contact_rule_file()))
    if mode not in blocks:
        mode = "revise"          # R5：缺省 = revise
    return blocks[mode]
