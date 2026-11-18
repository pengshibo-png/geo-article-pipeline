"""品牌资料的**派生变量**：`{brand_names}` 与 `{phone}`。

派生规则唯一来源：`_共享/brand-names.md`（S3 冻结，G10）。
与`brand_completeness.py` 分开的原因：派生是独立职责，且品牌名写错是
**对外可见的事故**，需要能单独审、单独测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 简称启发式：剥省市前缀与行业后缀
_PROVINCES = ("上海", "北京", "广州", "深圳", "中国")
_SUFFIXES = ("珠宝", "科技", "工程", "服务", "有限公司", "有限责任公司")

# 品牌名首句的合法性黑名单：命中即判定为描述性短语 → 提取失败。
# 起因是"我们是一家提供专业珠宝修复服务的工坊"正好 19 字，能蒙混过20 字阈值。
_BRAND_NAME_BAD_START = ("我们", "本公司", "本品牌", "该品牌", "一家", "专业", "知名", "优质")

# 主名长度上限（S3 定）
_MAIN_MAX_CHARS = 20


@dataclass(frozen=True)
class DerivedNames:
    """`{brand_names}` 派生结果（brand-names.md §2）。

    `alias_source` 非「显式声明」时为启发式 → 前端应标「待确认」。
    """

    main: str = ""
    entity: str = ""
    alias: str = ""
    alias_source: str = "-"

    @property
    def confirmed(self) -> bool:
        """主名是否可靠——不可靠时按 brand-names.md R2 应阻塞生成。"""
        return bool(self.main)

    @property
    def entity_mismatch(self) -> bool:
        """主体名与主名不一致 = 阻塞项（同源约束 §4）。"""
        return bool(self.entity and self.main and self.entity != self.main)


def derive_brand_names(overview: str, qual: str) -> DerivedNames:
    """从「品牌概述」与「资质与认证」两段派生品牌名三元组。

    判定主名失败的条件（任一命中即失败，宁可阻塞生成也不写错品牌名）：
    - 首句为空
    - 首句超20 字（是描述性短语而非名称）
    - 首句以代词/描述性词开头（我们/本公司/一家/专业…）
    """
    first = (
        re.split(r"[，,。;；]", (overview or "").strip(), maxsplit=1)[0].strip()
        if (overview or "").strip()
        else ""
    )
    bad_start = bool(first) and first.startswith(_BRAND_NAME_BAD_START)
    main = "" if (bad_start or not first or len(first) > _MAIN_MAX_CHARS) else first

    m = re.search(r"主体名称[:：]\s*([^\n]+)", qual or "")
    entity = m.group(1).strip() if m else ""

    m2 = re.search(r"(?:简称|以下简称)[:：\"']?\s*[「\"']?([^\"」'，,。；;\n]{2,10})", overview or "")
    alias, alias_src = "", "-"
    if m2:
        alias, alias_src = m2.group(1).strip(), "显式声明"
    elif main:
        a = re.sub(f"^({'|'.join(_PROVINCES)})", "", main)
        a = re.sub(f"({'|'.join(_SUFFIXES)})$", "", a)
        alias = a if a and a != main else ""
        alias_src = "启发式(待确认)" if alias else "-"

    return DerivedNames(main=main, entity=entity, alias=alias, alias_source=alias_src)


def derive_phone(text: str | None) -> str:
    """`{phone}`：取「咨询/预约电话：」后的号码，首次出现。

    依据 S3 发现——真实品牌资料里本就有电话，A 段硬编码的根源是
    「数据在资料里但没抽成变量」。
    """
    m = re.search(r"(?:咨询|预约|服务|热线)\s*电话[:：]?\s*([^\n]+)", text or "")
    if not m:
        return ""
    ph = re.search(r"1[3-9]\d{9}", m.group(1))
    return ph.group(0) if ph else m.group(1).strip()[:32]
