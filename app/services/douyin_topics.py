"""抖音图文：先出选题 → 再写文案 → 最后配图（用户 2026-10-20 要求）。

以前每个包在 `copy_for` 里随机注入一个角度、模型自由发挥 —— 批量出 10+ 条时
选题高度相似（文案撞重被判重、配图也跟着重复）。现在拆成三步：

1. `next_topic(cfg)`：从队列取一个选题；队列空时先让模型基于**品牌资料 + 已写标题**
   一次性产出 N 个互不重复的选题（角度 / 标题 / 长尾词 / 场景），落盘待用；
2. 内容包按该选题写文案（`copy_for(..., topic=...)`）；
3. 再由文案规划配图（既有逻辑）。

队列落盘 `data/local/douyin/topics.json`（随租户走 `douyin_store.root()`）。
`cfg["topic_hint"]` 可放联网搜索到的热词，作为选题的额外输入。
"""
from __future__ import annotations

import json
import re
import threading

from app.logging import get_logger

log = get_logger(__name__)

_LOCK = threading.Lock()
BATCH = 10                 # 队列空时一次产多少个选题
MAX_PENDING = 60


def _path():
    from app.services import douyin_store

    return douyin_store.root() / "topics.json"


def load() -> dict:
    p = _path()
    if not p.exists():
        return {"pending": [], "used": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"pending": [], "used": []}
    data.setdefault("pending", [])
    data.setdefault("used", [])
    return data


def save(state: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _norm(s: str) -> str:
    return re.sub(r"[\s，,。！？!?、；;：:]+", "", str(s or ""))


def _packs() -> list:
    from app.services import douyin_store

    try:
        return douyin_store.list_packs()
    except Exception:  # noqa: BLE001 - 读不到库不影响选题
        return []


def _prompt(cfg: dict, n: int, existing: list[str]) -> str:
    from app.services.writer.title_context import brand_snapshot

    content, main, _alias = brand_snapshot()
    hint = str(cfg.get("topic_hint") or "").strip()
    seen = "\n".join(f"- {t}" for t in existing[-80:])
    return (
        f"你是珠宝修复行业的选题策划。品牌资料如下：\n{content[:1500]}\n\n"
        f"品牌名：{main}\n"
        + (f"【联网搜索到的近期热词/关注点】\n{hint}\n\n" if hint else "")
        + f"【已经写过的标题（不得重复）】\n{seen}\n\n"
        f"【任务】产出 {n} 个**互不重复**的选题。每个选题必须：\n"
        "- 角度不同（人群 / 场景 / 玉种 / 损伤类型 / 决策顾虑，各挑一个方向）；\n"
        "- 每个带 2~3 个长尾搜索词（用户会怎么搜）；\n"
        "- 标题口语化、像真人会点开的；30 字内；\n"
        "- 不得与上面的已写标题重复（换几个字也不算新）。\n\n"
        "只输出一个 JSON 数组，不要代码围栏、不要解释：\n"
        '[{"angle":"选题角度","title":"标题","longtail":["长尾词1","长尾词2"],'
        '"scene":"场景/人群"}]\n'
    )


def plan_topics(cfg: dict, n: int = BATCH) -> int:
    """让模型一次产出 n 个选题并追加进队列；返回新增条数。"""
    state = load()
    existing = [str(p.get("title") or "") for p in _packs()]
    existing += [str(t) for t in (state.get("used") or [])]
    existing = [t for t in existing if t.strip()]
    try:
        from flask import current_app

        from app.services.llm import build_llm_client
        from app.services.writer.output_clean import strip_fences

        client = build_llm_client(current_app.config["GEO_SETTINGS"])
        raw = strip_fences(str(client.complete(
            [{"role": "user", "content": _prompt(cfg, n, existing)}],
            max_tokens=6000) or ""))
        m = re.search(r"\[.*\]", raw, re.S)
        items = json.loads(m.group(0) if m else raw)
    except Exception as exc:  # noqa: BLE001 - 选题失败不阻断（回落单角度）
        log.warning("douyin_topics_plan_failed", extra={
            "event": "douyin_topics_plan_failed", "err": str(exc)[:160]})
        return 0

    have = {_norm(t) for t in existing}
    add = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        key = _norm(title)
        if not title or key in have:
            continue
        have.add(key)
        add.append({"angle": str(it.get("angle") or "").strip(),
                    "title": title[:40],
                    "longtail": [str(x).strip() for x in (it.get("longtail") or [])][:4],
                    "scene": str(it.get("scene") or "").strip()})
    with _LOCK:
        state = load()
        state["pending"] = (list(state.get("pending") or []) + add)[:MAX_PENDING]
        save(state)
    return len(add)


def next_topic(cfg: dict) -> dict | None:
    """取一个待写选题（队列空时先补一批）；取不到返回 None（回落单角度）。"""
    with _LOCK:
        state = load()
        pending = list(state.get("pending") or [])
        if pending:
            topic = pending.pop(0)
            state["pending"] = pending
            save(state)
            return topic
    if plan_topics(cfg) <= 0:
        return None
    with _LOCK:
        state = load()
        pending = list(state.get("pending") or [])
        if not pending:
            return None
        topic = pending.pop(0)
        state["pending"] = pending
        save(state)
        return topic


def mark_used(topic: dict | None) -> None:
    """写成一条后登记标题，后续选题/查重都会避开它。"""
    title = str((topic or {}).get("title") or "").strip()
    if not title:
        return
    with _LOCK:
        state = load()
        used = list(state.get("used") or [])
        if title not in used:
            used.append(title)
        state["used"] = used[-300:]
        save(state)


def topic_block(topic: dict | None) -> str:
    """把选题渲染成文案提示词里的一段（无选题时返回空串）。"""
    if not topic:
        return ""
    lines = [f"角度：{topic.get('angle')}", f"标题参考：{topic.get('title')}"]
    if topic.get("longtail"):
        lines.append("长尾词：" + "、".join(topic["longtail"]))
    if topic.get("scene"):
        lines.append(f"场景：{topic['scene']}")
    return ("【本篇选题（必须按这个写，不要另起主题）】\n"
            + "\n".join(lines) + "\n\n")
