"""GEO 监测执行器：提问采样 → GeoResult 落库 → GeoDaily 当日聚合。

口径铁律（AI-ARCH-002）：同一引擎同一周期只用一种通道（channel 必填，本期 api）；
未提及则 rank 必须为 NULL（表级 CheckConstraint 守着）。**控制组**（is_control）
本期未启用，全部 False。

采样问句 = 选题库标题（"杭州断镯修复哪家好"本身就是用户会问 AI 的话）。
引擎经 LLM 客户端调用：真实环境按 S2 方案给每个引擎配独立端点；
本期用统一客户端（GEO_LLM_PROVIDER）逐引擎采样，fake 离线可测。

进度回报走进程内状态桥（与 services/writer/managed.py 同一约定：
锁不可重入，先取 state 引用再进锁）。
"""
from __future__ import annotations

import re
import threading

from flask import current_app

from app.logging import get_logger
from app.models import GeoResult, Tenant, Title
from app.platform.jobs import task
from app.platform.state_bridge import bridge
from app.services import geo_sim
from app.services.geo_sim import (  # noqa: F401  采样辅助下沉 geo_sim（A3 行数预算）
    _brand_name, _product, sample_engine as _sample_engine,
)
from app.services.llm import build_llm_client
from app.tenancy import TenantScopedRepository, current_tenant
from app.timeutil import now_local

log = get_logger(__name__)
ENGINES = ("doubao", "qwen", "yuanbao", "deepseek")

_LOCK = threading.Lock()
_LOG_CAP = 200
# tid → {"running","stop","log_tail","error","done","total"}
# **有界容器**（StateBridge）：超上限驱逐最久未访问的租户，避免只增不减的泄漏
_STATE = bridge(lambda: {"running": False, "stop": False, "log_tail": [],
                         "error": None, "done": 0, "total": 0})


def _state(tid: str) -> dict:
    return _STATE.get(tid)      # 有界容器：超上限驱逐最久未访问的租户


def begin(tid: str, total: int) -> None:
    st = _state(tid)
    with _LOCK:
        st.update(running=True, stop=False, error=None, done=0, total=total)
        st["log_tail"] = []


def is_running(tid: str) -> bool:
    return bool(_state(tid)["running"])


def request_stop(tid: str) -> None:
    _state(tid)["stop"] = True


def _log(st: dict, line: str) -> None:
    with _LOCK:
        st["log_tail"].append(line)
        del st["log_tail"][:-_LOG_CAP]


def snapshot(tid: str) -> dict:
    st = _state(tid)
    with _LOCK:
        return {"running": st["running"], "log_tail": list(st["log_tail"][-40:]),
                "error": st["error"],
                "progress": {"total": st["total"], "done": st["done"],
                             "success": st["done"], "fail": 0}}


def reset_states() -> None:
    with _LOCK:
        _STATE.clear()


# ── 响应判定 ────────────────────────────────────────────────────────────────

# 大小写不敏感：模型回答里会输出 "HTTPS://..."（scheme 大写），否则整条漏判
_URL_RE = re.compile(r"https?://[^\s\"'<>)）]+", re.I)


def _detect_rank(resp: str, brand: str):
    """排名 = 品牌出现的编号行（'1. xxx 2. xxx'）；提及但无法定位 → None（合法）。"""
    if brand not in resp:
        return None
    for m in re.finditer(r"(?m)^\s*(\d+)[.、)]\s*(.+)$", resp):
        if brand in m.group(2):
            return int(m.group(1))
    return None


# 演示/离线域（fake 驱动与演示数据的链接也要能被判为被引，否则演示态永远 0）
_MOCK_HOSTS = ("fake.example.com", "meipian")


def _norm_url(url: str) -> str:
    """URL 归一：去尾部标点与斜杠、小写（AI 回答里常带句号/括号/大小写差异）。"""
    return (url or "").strip().rstrip("/.,);、。）】").lower()


def _url_key(url: str) -> str:
    """比对用键：忽略 scheme、大小写、尾斜杠、查询串与锚点。

    AI 回答里的链接常带 `?utm_source=…` / `#锚点` / 末尾句号，直接比字符串会漏。
    """
    u = re.sub(r"^https?://", "", _norm_url(url))
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def _is_cited(url: str, known_keys) -> bool:
    """该 URL 是否属于"本租户发布过的内容"（known_keys = 归一后的键集合）。

    **精确匹配 + O(1) 集合查找**。之前用双向子串包含（`nk in nu or nu in nk`）：
    短 URL（如 `https://a.com`）会命中任意含它的长 URL，把别人的文章误判成
    "被引"、让被引率虚高；且对每个 URL 遍历全部发布记录是 O(N×M)，发布记录上千
    后单轮监测会卡。
    """
    if not url:
        return False
    if any(h in url.lower() for h in _MOCK_HOSTS):
        return True
    return _url_key(url) in known_keys


def _known_url_keys() -> set[str]:
    """本租户**真实发布成功**过的 URL（归一为比对键）—— 被引判定的依据。

    真实渠道（搜狐/百家号/列表网/58/蚁小二）发出去的链接各不相同，写死域名
    白名单必然漏判，故以租户实际发布记录为准（PublishLog.url）。
    """
    from app.models import PublishLog

    try:
        rows = TenantScopedRepository(PublishLog).query().filter(
            PublishLog.success.is_(True),
            PublishLog.url.isnot(None), PublishLog.url != "").all()
    except Exception:  # noqa: BLE001 - 取不到发布记录则退化为仅演示域（不中断监测）
        return set()
    return {_url_key(r.url) for r in rows if r.url}


def _engine_clients() -> dict:
    """四引擎各自的 LLM 客户端（配了独立端点才建；没配的不进字典 → 退回统一客户端）。

    引擎 Key 缺失或装配失败只记日志、跳过该引擎（它随后用统一客户端问，仍出数），
    不中断整轮监测。
    """
    from app.services.llm import build_engine_client
    from app.services.model_config import effective_engine

    out: dict = {}
    for e in ENGINES:
        cfg = effective_engine(e)
        if not cfg:
            continue
        try:
            out[e] = build_engine_client(cfg)
        except Exception as exc:  # noqa: BLE001 - 单引擎装配失败不拖垮整轮
            log.warning("geo_engine_client_failed", extra={
                "event": "geo_engine_client_failed", "engine": e, "err": str(exc)[:120]})
    return out


def _detect_cite(resp: str, known=frozenset()) -> str | None:
    """被引 = 回答里出现本租户发布过的链接（或演示域）。"""
    for m in _URL_RE.finditer(resp or ""):
        url = m.group(0)
        if _is_cited(url, known):
            return url
    return None


# 聚合口径（recompute_daily / dashboard）拆在 app/services/geo_aggregate.py
# （A3 行数预算）；此处 re-export 保持既有调用点与测试导入不变。
from app.services.geo_aggregate import (  # noqa: F401,E402  re-export
    _metrics, dashboard, recompute_daily,
)


# ── 采样任务 ────────────────────────────────────────────────────────────────

@task("geo.run")
def geo_run_job(payload: dict) -> dict:
    """虚拟收录查询（模拟各平台回答）—— 监测模块的唯一采样入口。

    不再"裸调各引擎冒充真实引用"：改为**模拟**每个平台对该品类的回答倾向
    （平台人设 + 知名度锚定 + 平台间 ±8% 波动），全部落 `channel='simulate'`，
    如实标注为模拟。问句来自选题库标题（本身就是用户会问 AI 的话）。
    """
    tid = current_tenant()
    st = _state(tid)
    engines = [e for e in (payload.get("engines") or []) if e in ENGINES] or list(ENGINES)
    rounds = max(1, min(int(payload.get("rounds") or 1), 10))
    limit = max(0, int(payload.get("limit") or 0))

    repo = TenantScopedRepository(Title)
    queries = [r.text for r in repo.query().filter(Title.deleted_at.is_(None)).all()]
    if limit:
        queries = queries[:limit]
    if not queries:
        _log(st, "⚠ 选题库为空：没有可测提问词，请先在「选题库」生成或添加选题")
        with _LOCK:
            st["running"] = False
        return {"ok": True, "sampled": 0}

    brand = _brand_name(tid)
    product = _product()
    llm = build_llm_client(current_app.config["GEO_SETTINGS"])

    with _LOCK:
        st["total"] = len(engines) * rounds
    fame = geo_sim.classify_fame(llm, _brand_content(), product, brand)
    _log(st, f"🎭 模拟模式（虚拟收录查询）· 知名度档位：{fame} · "
             f"{len(queries)} 问句 × {len(engines)} 引擎 × {rounds} 轮")

    grepo = TenantScopedRepository(GeoResult)
    sampled = 0
    try:
        for _round in range(rounds):
            for engine in engines:
                if st["stop"]:
                    break
                rows = _sample_engine(llm, engine, queries, brand, product, fame)
                for gresult in rows:
                    grepo.add(gresult)
                    from app.extensions import db as _db
                    _db.session.flush()
                    sampled += 1
                if rows and rows[0].error:
                    _log(st, f"❌ {engine}：{rows[0].error[:50]}")
                else:
                    hit = sum(1 for r in rows if r.mentioned)
                    _log(st, f"✅ {engine}：模拟提及 {hit}/{len(rows)}")
                with _LOCK:
                    st["done"] += 1
            if st["stop"]:
                _log(st, "⏸ 收到停止指令，本轮监测结束")
                break
    except Exception as exc:  # noqa: BLE001 - 单引擎失败不中断整轮
        with _LOCK:
            st["error"] = str(exc)
        _log(st, f"❌ 监测异常：{exc}")
    finally:
        with _LOCK:
            st["running"] = False

    days = recompute_daily()
    _log(st, f"📊 本轮采样 {sampled} 条（模拟）· 已聚合 {days} 个引擎日行")
    log.info("geo_run_done", extra={"event": "geo_run_done", "sampled": sampled})
    return {"ok": True, "sampled": sampled}


def _brand_content() -> str:
    from app.models import Brand

    row = TenantScopedRepository(Brand).first()
    return (row.content if row else "") or ""


def _sample(llm, query: str, engine: str, brand: str, known=frozenset()) -> GeoResult:
    gresult = GeoResult(query=query[:255], engine=engine, channel="api",
                        mentioned=False, sampled_at=now_local(), tenant_id=current_tenant())
    try:
        resp = llm.complete([{"role": "user", "content": query}])
    except Exception as exc:  # noqa: BLE001 - 单样本失败记录 error，不中断整轮
        gresult.error = str(exc)[:500]
        return gresult
    if brand and brand in resp:
        gresult.mentioned = True
        gresult.rank = _detect_rank(resp, brand)
    url = _detect_cite(resp, known)
    if url:
        gresult.cited_lv = True
        gresult.cited_url = url[:512]
    return gresult


__all__ = ["ENGINES", "begin", "is_running", "request_stop", "snapshot",
           "recompute_daily", "geo_run_job", "reset_states", "dashboard"]
