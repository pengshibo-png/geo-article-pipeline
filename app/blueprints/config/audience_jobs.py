"""客户画像 AI 洞察任务：LLM 分析画像 → 分节洞察文本。

状态桥与 douyin/video 同构（进程内 dict + 锁，锁不可重入：先取引用再进锁）。
洞察落 `data/<租户>/audience/insight.json`（audience_store），页面轮询取用。
"""
from __future__ import annotations

import threading

from flask import current_app

from app.logging import get_logger
from app.platform.jobs import task
from app.platform.state_bridge import bridge
from app.services.llm import build_llm_client
from app.tenancy import TenantScopedRepository, current_tenant

log = get_logger(__name__)
_LOCK = threading.Lock()
_LOG_CAP = 20
_STATE = bridge(lambda: {"analyzing": False, "log_tail": [], "error": None,
                         "result": ""})

_INSIGHT_PROMPT = """你是本地生活服务营销分析师。请基于以下客户画像，输出结构化洞察。

画像：
{profile}

要求：
1. 输出 **3-4 个小节**，每节以【小节名】开头（例如【人群特征】【内容偏好】【触达建议】【风险提醒】）；
2. 每节 2-4 条，每条一行、以「- 」开头，直接给结论，不写套话；
3. 全部基于上面画像的具体内容推导，画像没提到的不要编造；
4. 只输出小节与条目，不要前言、不要总结、不要 markdown 代码块围栏。"""


def _st(tid: str) -> dict:
    return _STATE.get(tid)      # 有界容器：超上限驱逐最久未访问的租户


def _log(st: dict, line: str) -> None:
    """日志入队（**在锁内**改 list：并发 append/del 否则会 IndexError/丢行）。

    锁不可重入 —— 调用方不得持有 _LOCK 时调本函数。
    """
    with _LOCK:
        st["log_tail"].append(line)
        del st["log_tail"][:-_LOG_CAP]


def state(tid: str) -> dict:
    """供 /api/audience/analyze-status：{analyzing, log_tail, error, result}。"""
    with _LOCK:
        s = _STATE.get(tid)
        if s is None:
            return {"analyzing": False, "log_tail": [], "error": None, "result": ""}
        return {"analyzing": s["analyzing"], "log_tail": list(s["log_tail"]),
                "error": s["error"], "result": s["result"]}


def try_begin(tid: str) -> bool:
    """原子抢占：**判重与置位在同一把锁内**（分开做是 TOCTOU，连点会双开）。"""
    st = _st(tid)
    with _LOCK:
        if st["analyzing"]:
            return False
        st["analyzing"] = True
        st["log_tail"] = []
        st["error"] = None
        st["result"] = ""
        return True


@task("audience.analyze")
def analyze_job(payload: dict) -> dict:
    tid = current_tenant()
    st = _st(tid)
    try:
        _log(st, "读取客户画像…")
        from app.models import Audience

        row = TenantScopedRepository(Audience).first()
        content = (row.content if row else "") or ""
        if not content.strip():
            raise ValueError("请先填写并保存客户画像，再生成洞察")
        _log(st, f"画像 {len(content)} 字，调用 AI 分析（人群/偏好/触达建议）…")
        llm = build_llm_client(current_app.config["GEO_SETTINGS"])
        raw = llm.complete([{"role": "user",
                             "content": _INSIGHT_PROMPT.format(profile=content[:3000])}])
        from app.services.writer.output_clean import clean_sections, strip_fences

        insight = clean_sections(raw or "")
        if not insight:
            # 模型没按【小节】格式给：退化为整段文本，不强求格式（页面仍可读）
            insight = strip_fences(raw or "").strip()
        if not insight:
            raise ValueError("AI 返回为空，请重试")
        from app.services import audience_store

        audience_store.save_insight(insight)
        with _LOCK:
            st["result"] = insight
        _log(st, "✅ 洞察已生成并自动注入后续文章")
    except Exception as exc:  # noqa: BLE001 - 失败如实写入状态，页面 alert
        with _LOCK:
            st["error"] = str(exc)[:200]
        _log(st, f"❌ 分析失败：{str(exc)[:80]}")
        log.warning("audience_analyze_failed",
                    extra={"event": "audience_analyze_failed", "tenant": tid,
                           "err": str(exc)[:120]})
    finally:
        with _LOCK:
            st["analyzing"] = False
    return {"ok": True}


__all__ = ["analyze_job", "state", "try_begin"]
