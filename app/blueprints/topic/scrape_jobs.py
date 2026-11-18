"""同行挖掘任务：LLM 产出同行会写的标题 → 写入 CompetitorTitle（学习库）。

诚实口径：本系统**不爬取外部站点**（无爬虫/搜索 API 授权），这里由 LLM 按
本租户业务与城市推演同行常见标题，作为"AI 智能选题"的训练材料 —— 与页面
文案一致（"这些是 AI 生成新选题的训练材料"），不冒充真实抓取。

状态桥与 douyin/video 同构（进程内 dict + 锁，锁不可重入：先取引用再进锁）。
"""
from __future__ import annotations

import threading

from flask import current_app

from app.logging import get_logger
from app.platform.jobs import task
from app.platform.state_bridge import bridge
from app.services.llm import build_llm_client
from app.services.writer.output_clean import clean_title_line
from app.tenancy import TenantScopedRepository, current_tenant

log = get_logger(__name__)
_LOCK = threading.Lock()
_LOG_CAP = 20
_LIBRARY_CAP = 500          # 学习库上限：超出淘汰最旧，避免无限膨胀
_STATE = bridge(lambda: {"running": False, "log_tail": [], "captured": 0,
                         "error": None})

_SCRAPE_PROMPT = """你是本地生活服务内容运营。请推演：在下面的业务与城市里，同行（其他商家）最常发的文章标题长什么样。

业务：{business}
城市：{cities}

要求：
1. 每行一个标题，共 {count} 行；
2. 要像同行真实会发的内容：带城市名、带用户会搜的口语问法、带攻略/避坑/报价/排行等常见套路；
3. 每行直接给标题，不要编号、不要加粗、不要引号；
4. 只输出标题行，不要任何前言与解释。"""


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
    """供 /api/titles/scrape-status：{running, log_tail, captured, error}。"""
    with _LOCK:
        s = _STATE.get(tid)
        if s is None:
            return {"running": False, "log_tail": [], "captured": 0, "error": None}
        return {"running": s["running"], "log_tail": list(s["log_tail"]),
                "captured": s["captured"], "error": s["error"]}


def try_begin(tid: str) -> bool:
    """原子抢占：**判重与置位在同一把锁内**。

    分开做（先 can_start 再在任务体里置 running）是典型 TOCTOU：连点两次
    POST 会双开两个任务，产出重复数据。
    """
    st = _st(tid)
    with _LOCK:
        if st["running"]:
            return False
        st["running"] = True
        st["log_tail"] = []
        st["captured"] = 0
        st["error"] = None
        return True


def _store_titles(titles: list[str]) -> int:
    """入库：去重 + 容量上限（学习库是滚动窗口，不是无限堆积）。

    去重口径：与库内已有标题比对，且本轮内部也去重。
    上限：超出 _LIBRARY_CAP 时淘汰最旧的（按 id 升序）。AI 每轮都会产出新标题，
    不去重也不清老会让 competitor_title 表无限膨胀、拖慢"AI 智能选题"。
    """
    from app.extensions import db
    from app.models import CompetitorTitle

    repo = TenantScopedRepository(CompetitorTitle)
    existing = {str(r.text or "") for r in repo.all()}
    fresh: list[str] = []
    for t in titles:
        if t in existing:
            continue
        existing.add(t)
        fresh.append(t)
    if not fresh:
        return 0
    rows = sorted(repo.all(), key=lambda r: r.id)
    overflow = len(rows) + len(fresh) - _LIBRARY_CAP
    for r in rows[:max(0, overflow)]:
        db.session.delete(r)
    for t in fresh:
        repo.add(CompetitorTitle(text=t[:255], source="ai"))
    db.session.commit()
    return len(fresh)


def _library_size() -> int:
    from app.models import CompetitorTitle

    return len(TenantScopedRepository(CompetitorTitle).all())


@task("titles.scrape")
def scrape_job(payload: dict) -> dict:
    tid = current_tenant()
    st = _st(tid)
    count = max(1, min(int(payload.get("count") or 20), 50))
    try:
        from app.models import Brand, TitleConfig

        cfg = TenantScopedRepository(TitleConfig).first()
        business = ((cfg.business if cfg else "") or "").strip()
        cities = ((cfg.cities if cfg else "") or "").strip()
        if not business:
            brand = TenantScopedRepository(Brand).first()
            business = ((brand.content if brand else "") or "")[:40] or "（未配置业务）"
        _log(st, f"按业务「{business[:20]}」推演同行常见标题…")
        llm = build_llm_client(current_app.config["GEO_SETTINGS"])
        raw = llm.complete([{"role": "user", "content": _SCRAPE_PROMPT.format(
            business=business[:120], cities=(cities or "全国")[:120], count=count)}])
        titles = [t for t in (clean_title_line(ln) for ln in (raw or "").splitlines()) if t]
        if not titles:
            raise ValueError("AI 未产出任何标题，请重试")
        added = _store_titles(titles[:count])
        with _LOCK:
            st["captured"] = added
        _log(st, f"✅ 本轮入库 {added} 条（去重后），学习库累计 {_library_size()} 条")
        log.info("titles_scrape_done", extra={"event": "titles_scrape_done",
                                              "tenant": tid, "captured": st["captured"]})
    except Exception as exc:  # noqa: BLE001 - 失败如实写入状态，页面 alert
        with _LOCK:
            st["error"] = str(exc)[:200]
        _log(st, f"❌ 挖掘失败：{str(exc)[:80]}")
        log.warning("titles_scrape_failed", extra={
            "event": "titles_scrape_failed", "tenant": tid, "err": str(exc)[:120]})
    finally:
        with _LOCK:
            st["running"] = False
    return {"ok": True}


__all__ = ["scrape_job", "state", "try_begin"]
