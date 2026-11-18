"""抖音图文的质量关卡：文案结构校验 + **重复查重**。

拆成独立模块的原因：`douyin_pack.py` 已接近 300 行门禁（A3），而这些校验
逻辑与"组装"无关，放一起会撞线。

## 这里解决的两个实测问题（2026-10-19）

1. **文案写到一半**：8 个包里 3 个是半截（251 字 / 884 字 / 892 字），
   共同特征是"结尾没有固定尾行"。
2. **作品大量重复**：46 个内容包只有 17 个不同标题 ——
   `苏州·上门服务实录` ×11、`杭州·上门服务实录` ×10（LLM 失败时的固定兜底），
   `玉镯断了还能不能戴？` ×12（LLM 反复产出同一标题，零查重）。
"""
from __future__ import annotations

import itertools
import re
import threading

# ── 文案结构校验（"真的写完了"没有）─────────────────────────────────────────
_RANGE_RE = re.compile(r"(\d{2,4})\s*[–\-~至]\s*(\d{2,4})\s*字")
_FOOTER_RE = re.compile(r"最后一行固定[:：]\s*(.+)")
_END_PUNCT = "。！？…\"'」』）)】."
# 查重时只比对正文前 N 字：同一选题换写法不算重复，整段照抄才算
_SAMPLE_CHARS = 40
# 判定"重复"的共同片段长度：正文连续 12 字相同即视为照抄
_DUP_RUN = 12
# 标题更短，用整句相等/包含来判（"玉镯断了还能不能戴？"只有 10 字，够不上 12 字片段）
_TITLE_MIN = 6


def expected_footer(cfg: dict) -> str:
    """从 copy_prompt 的「最后一行固定：xxx」里抠出尾行（配置驱动，不硬编码）。"""
    m = _FOOTER_RE.search(str(cfg.get("copy_prompt") or ""))
    return m.group(1).strip().rstrip("。") if m else ""


def copy_floor(cfg: dict) -> int:
    """从 copy_prompt 的「正文 600–900 字」读出字数下限（取 80% 防误杀）。

    不能写死：字数要求是用户写在产线配置里的，配短文案时硬下限会误杀。
    读不到返回 0 —— 不做字数校验。
    """
    m = _RANGE_RE.search(str(cfg.get("copy_prompt") or ""))
    return int(int(m.group(1)) * 0.8) if m else 0


def copy_problems(desc: str, footer: str, floor: int = 0) -> list[str]:
    """结构校验：文案是否"真的写完了"。返回问题清单，空列表 = 合格。"""
    body = (desc or "").strip()
    if not body:
        return ["正文为空"]
    problems: list[str] = []
    chars = len(re.sub(r"\s", "", body))
    if floor and chars < floor:
        problems.append(f"正文过短（{chars} 字，应为 {floor} 字以上）")
    if footer:
        if not body.endswith(footer):
            problems.append("结尾缺少固定尾行（正文没写完）")
    elif floor and body[-1] not in _END_PUNCT:
        problems.append("结尾不是标点（疑似被截断）")
    return problems


def incomplete_hint(problems: list[str], footer: str, floor: int = 0) -> str:
    """把具体问题喂回给模型重写一次（不抽象说"再完整点"）。"""
    lines = ["", "", "【上一次输出不合格，请完整重写】"]
    lines += [f"- {p}" for p in problems]
    if floor:
        lines.append(f"- 正文应不少于 {floor} 字")
    if footer:
        lines.append(f"- **最后一行必须是**：{footer}")
    lines.append("- 只输出 JSON，不要解释；务必把结尾写完，不要省略或截断。")
    return "\n".join(lines)


# ── 重复查重（"不要又生成同一篇"）──────────────────────────────────────────

def sample_of(text: str) -> str:
    """查重样本：标题 + 正文前 _SAMPLE_CHARS 字（去空白）。"""
    body = re.sub(r"\s", "", str(text or ""))
    return body[:_SAMPLE_CHARS]


def dup_hits(title: str, desc: str, existing: list[str]) -> list[str]:
    """返回与既存样本重复的条目。

    - **标题**：**整句完全相同**才算重复（2026-10-19 放宽）。
      以前"互相包含"也算重，而 `existing` 里混着正文片段样本，新标题只要被某段
      正文前 40 字包含就误判 —— 实测正常稿被大量拦下、3 次全拒后回落兜底。
    - **正文**：连续 `_DUP_RUN` 字相同才算照抄（同一选题换写法不算重复）

    `existing` 是 `samples()` 的产物（标题样本 + 正文片段样本各一份）。
    """
    head = sample_of(title)
    body = sample_of(desc)
    hits: list[str] = []
    for s in existing:
        if not s:
            continue
        if len(head) >= _TITLE_MIN and len(s) >= _TITLE_MIN and head == s:
            hits.append(s)
        elif body and _shared_run(body, s) >= _DUP_RUN:
            hits.append(s)
    return hits


def _shared_run(a: str, b: str) -> int:
    """a、b 之间最长的共同连续片段长度。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while (i + k < len(a) and j + k < len(b)
                   and a[i + k] == b[j + k]):
                k += 1
            best = max(best, k)
            if best >= _DUP_RUN:
                return best
    return best


def dedup_hint(hits: list[str], limit: int = 8) -> str:
    """把已重复的既有标题喂回去，让模型换个角度写。"""
    if not hits:
        return ""
    lines = ["", "",
             "【上一次输出与既有内容重复，必须换一个全新角度】",
             "以下标题/正文已经存在，**不许重复或高度相似**："]
    for h in hits[:limit]:
        lines.append(f"- {h[:44]}")
    lines.append("请换一个不同的损伤类型 / 工艺 / 判断维度来写。")
    return "\n".join(lines)


def samples(packs: list[dict], limit: int = 200) -> list[str]:
    """从既有内容包里取查重样本（标题 + 正文片段）。"""
    out: list[str] = []
    for p in (packs or [])[-limit:]:
        title = sample_of(p.get("title") or "")
        if title:
            out.append(title)
        desc = sample_of(p.get("description") or "")
        if desc and desc != title:
            out.append(desc)
    return out


# ── 选题角度轮换（B：扩充选题，避免反复撞既有标题）──────────────────────────
# 既有 80+ 个包已覆盖主流修复选题，只靠通用提示词时模型会反复产出同样的标题。
# 每次生成随机注入一个**尚未被写过的角度**，把它变成"这一篇写什么"的明确指令。
TOPIC_ANGLES = [
    "婚嫁三金手镯断了，修还是重新买",
    "传家的老玉镯裂了，修旧如旧怎么把握",
    "旅游景点买的手镯裂了，值不值得寄修",
    "直播间买的手镯收到就有暗裂，退还是修",
    "手镯内圈裂了，继续戴会不会断",
    "条杆偏细的手镯断了，能不能加金加固",
    "雕花位置崩口，修复会不会磨平花纹",
    "翡翠和和田玉断了，修复工艺差在哪",
    "手镯修复后能不能碰水，日常怎么保养",
    "修复处会不会再裂，质保怎么算",
    "寄修手镯怎么打包，保价和拍照留证怎么做",
    "断镯不想修了，改成吊坠或戒面怎么选",
    "断成三截以上的手镯，还有修的价值吗",
    "镶金和包金修复，哪种更结实",
    "手镯修复一般要多久，能不能加急",
    "老人留下的镯子断了，想修好继续戴",
    "手镯断口发黑，是不是就不能修了",
    "同价位，是修旧镯还是买新镯",
    "修复前要不要先做材质鉴定",
    "断镯修好后重量会变吗，会影响价值吗",
    "手镯戴久了变形，能不能校正",
    "翡翠手镯摔断，镶金后还能不能戴",
    "断镯修复前要不要拍照留证",
    "手镯修复后颜色会不会变",
    "金镶玉手镯摔了怎么修",
    "手镯裂到内圈，还能不能抛光",
    "老银镯变形发黑，值不值得修",
    "和田玉手镯磕了个坑能补吗",
    "手镯断口有缺口，补料用什么",
    "玉镯修复收费一般分几档",
    "修复过的镯子还能不能传家",
    "手镯摔断后多久修最好",
    "镯子断在雕花处，花纹还能复原吗",
    "修复后镯子圈口会不会变小",
    "网上买的镯子断了找谁修",
]

_ANGLE_SEQ = itertools.count()
_ANGLE_LOCK = threading.Lock()


def topic_angle() -> str:
    """按顺序轮换选题角度（B：扩充选题）。

    以前是 `random.choice`：批量生成 20+ 条时同一个角度会反复出现，文案跟着重复
    → 判重 → 兜底（甚至被"兜底不进配图"拦下）。改成进程内顺序轮换，
    一轮用完再从头开始，批量出稿的选题天然不撞。
    """
    with _ANGLE_LOCK:
        idx = next(_ANGLE_SEQ)
    return TOPIC_ANGLES[idx % len(TOPIC_ANGLES)]
