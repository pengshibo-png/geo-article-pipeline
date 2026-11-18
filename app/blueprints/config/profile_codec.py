"""客户画像 行式文本 ↔ 结构对象 编解码。

Audience.content 是**一行式文本**（生成侧 {audience} 变量直接引用），
「客户画像」页编辑的是结构对象 `{singles, lists, customs}`。两侧各取所需：
行式文本对人/对 Prompt 友好，结构对象对表单友好。约定格式：

    【人群描述】30-55 岁业主…          ← 单值字段：标题行内联
    【核心痛点】                        ← 多值字段/自定义参数：标题行 + "- " 条目行
    - 反复漏水总修不好
    【自定义：咨询高频问题】
    - 价格怎么算

单值行也允许「- 」条目（容错旧数据），解码时并入 lists 里同名标签。
标签映射与原站页面 SINGLES/LISTS 常量逐字一致。
"""
from __future__ import annotations

# key → 展示标签（页面 SINGLES 顺序与文案）
SINGLE_LABELS: dict[str, str] = {
    "who": "人群描述", "age": "年龄段", "gender": "性别比例", "area": "地域范围",
    "budget": "消费能力", "job": "职业身份", "channel": "了解渠道", "decider": "决策角色",
    "content": "信息偏好", "freq": "消费频次",
}
_LABEL_TO_KEY = {v: k for k, v in SINGLE_LABELS.items()}
# 多值字段标签（页面 LISTS）
LIST_LABELS: dict[str, str] = {
    "pain": "核心痛点", "worry": "决策顾虑", "scene": "使用场景",
    "places": "常见地点", "objection": "犹豫 / 拒绝理由",
}
_LABEL_TO_LIST = {v: k for k, v in LIST_LABELS.items()}
_CUSTOM_PREFIX = "自定义："


def serialize_profile(profile: dict) -> str:
    """结构对象 → 行式文本。空 profile 返回空串。"""
    singles = profile.get("singles") or {}
    lists = profile.get("lists") or {}
    customs = profile.get("customs") or []

    lines: list[str] = []
    for key, label in SINGLE_LABELS.items():
        v = str(singles.get(key) or "").strip()
        if v:
            lines.append(f"【{label}】{v}")
    for key, label in LIST_LABELS.items():
        items = [str(i).strip() for i in (lists.get(key) or []) if str(i).strip()]
        if items:
            lines.append(f"【{label}】")
            lines.extend(f"- {i}" for i in items)
    for c in customs:
        label = str(c.get("label") or "").strip()
        items = [str(i).strip() for i in (c.get("items") or []) if str(i).strip()]
        if not label or not items:
            continue
        lines.append(f"【{_CUSTOM_PREFIX}{label}】")
        lines.extend(f"- {i}" for i in items)
    return "\n".join(lines)


def deserialize_profile(text: str | None) -> dict:
    """行式文本 → 结构对象。非约定格式的自由文本归入 singles.who（人群描述），
    保证旧数据不静默丢失——页面首屏可见、可编辑、保存后转为新格式。"""
    singles: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    customs: list[dict] = []
    cur_single: str | None = None
    cur_list: list[str] | None = None
    free_text: list[str] = []

    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("【") and "】" in line:
            label = line[line.index("【") + 1: line.index("】")]
            inline = line[line.index("】") + 1:].strip()
            if label in _LABEL_TO_KEY:
                cur_single = _LABEL_TO_KEY[label]
                cur_list = None
                if inline:
                    singles[cur_single] = inline
                continue
            if label in _LABEL_TO_LIST:
                cur_single = None
                cur_list = lists.setdefault(_LABEL_TO_LIST[label], [])
                if inline:
                    cur_list.append(inline)
                continue
            if label.startswith(_CUSTOM_PREFIX):
                cur_single = None
                cur_list = []
                customs.append({"label": label[len(_CUSTOM_PREFIX):].strip(), "items": cur_list})
                if inline:
                    cur_list.append(inline)
                continue
            # 未知【标签】：整体按条目并入犹豫理由兜底？——不，并入自由文本
            cur_single = None
            cur_list = None
            free_text.append(line)
            continue
        if line.strip().startswith("-"):
            item = line.strip().lstrip("-").strip()
            if not item:
                continue
            if cur_list is not None:
                cur_list.append(item)
            elif cur_single is not None:
                singles[cur_single] = (singles.get(cur_single, "") + "\n" + item).strip()
            else:
                free_text.append(line)
            continue
        if cur_single is not None:
            singles[cur_single] = (singles.get(cur_single, "") + "\n" + line.strip()).strip()
        elif cur_list is not None:
            cur_list.append(line.strip())
        else:
            free_text.append(line)

    who = singles.get("who", "")
    extra = "\n".join(free_text).strip()
    if extra:
        singles["who"] = (who + "\n" + extra).strip() if who else extra
    return {"singles": singles, "lists": lists, "customs": customs}


__all__ = ["serialize_profile", "deserialize_profile", "SINGLE_LABELS", "LIST_LABELS"]
