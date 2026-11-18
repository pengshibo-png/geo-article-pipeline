"""重写环的**兜底动作**：降级交付、回退初稿、重写前快照。

从 `rewrite.py` 拆出（A3 单文件 300 行预算）。这条线的主题只有一个 ——
**"流水线走不通时，怎么保证这篇文章还在"**：

- `_deliver_degraded`：重写额度用尽 / 后续阶段硬失败 → 不作废整篇，尽力修订一次
  后交付，并**如实标注"未通过审核"**（审稿结论原样留在 02_review.json）；
- `_revert_to_draft`：定稿复评反而更差（<60）→ 用初稿替换定稿交付。它会**直接
  替换已落盘的 03_final.html**（静默数据变更），所以返回动作字符串由调用方记账，
  且已发布（status=published）的一律不覆盖；
- `_snapshot_for_rewrite` / `_restore_snapshot`：重写轮必须先删上一轮产物才能让
  A 段重跑，删之前把**唯一副本**读进内存，生成失败时写回 —— 否则"生成失败"
  等于"文章目录变空"，文章凭空消失。

三条统一约束：**不抛异常给成功路径**（调用点外面没有兜底）、**不伪装**
（降级事实与审稿结论都留痕）、**不覆盖已发布**（磁盘那份可能已经分发出去）。
"""
from __future__ import annotations

import json

from app.logging import get_logger
from app.platform import storage

log = get_logger(__name__)


def _read_review(dir_name: str, base) -> dict:
    try:
        return json.loads(storage.read_article_file(dir_name, storage.REVIEW, base=base))
    except Exception:  # noqa: BLE001 - 读不到/格式坏都当空审稿，仍照常重写
        return {}


def _title_of(dir_name: str) -> str:
    """取文章标题（续跑场景 payload 不带 title 时用）。"""
    from app.models import Article
    from app.tenancy import TenantScopedRepository

    row = TenantScopedRepository(Article).first(dir=dir_name)
    return str((row.title if row else "") or "") or dir_name


def _notify(fn, message: str) -> None:
    if fn is not None:
        fn(message)


def _mark_final(dir_name: str, *, degraded: bool = False) -> None:
    """把档案状态推到终态。

    ★ **降级交付（未过审）绝不标 final**：定时发布计划只按 `status == "final"`
    取稿自动分发（scheduler.py:249），标 final 等于把没过审的稿子直接发到客户
    账号 —— 而 Article 表没有 degraded 字段，发布链路无从区分。
    降级稿统一标 `reviewed`：档案页照样可见、可人工确认后再发，但不进自动队列。
    """
    from app.extensions import db
    from app.services.writer.pipeline import _upsert_article
    from app.timeutil import now_local

    row = _upsert_article(dir_name, dir_name)
    if row.status != "published":
        row.status = "reviewed" if degraded else "final"
    row.final_at = now_local()
    db.session.commit()          # 即写即交


def _deliver_degraded(dir_name: str, base, contact_mode: str, review: dict,
                      stages: list, notify, reason: str) -> dict:
    """**降级交付**：不作废整篇，尽力产出可用文章，并如实标注未过审。

    顺序（**最多额外 1 次 LLM 调用**）：
    1. 有审稿结论 → 让 C 段做一次尽力修订（`degraded=True`，冻结规则的唯一例外）；
    2. 连修订都失败 / 根本没有审稿 → 用最后一版初稿直接作定稿（**0 次调用**）。
       初稿本身是 A 段按完整规范写出的成稿，可用。

    绝不伪装：审稿结论原样留在 02_review.json（verdict=建议重写 + 真实分数），
    页面日志与 app.log 都明确写出"降级交付"。
    """
    from app.services.writer.review import revise_article

    score = review.get("overall_score")
    _notify(notify, f"⚠ {reason} → 降级交付：尽力修订一次后交付（**未通过审核**）")
    source = ""
    if review:
        try:
            out = revise_article(dir_name, contact_mode=contact_mode, base=base,
                                 degraded=True)
            source = str(out.get("source") or "degraded-revise")
            stages.append(out)
        except Exception as exc:  # noqa: BLE001 - 连尽力修订都失败 → 回落到初稿
            log.warning("article_degraded_revise_failed", extra={
                "event": "article_degraded_revise_failed", "dir": dir_name,
                "err": f"{type(exc).__name__}: {str(exc)[:200]}"})
    if not source:
        draft = ""
        try:
            draft = storage.read_article_file(dir_name, storage.DRAFT, base=base)
        except Exception:  # noqa: BLE001
            draft = ""
        if not draft.strip():
            raise RuntimeError(f"降级交付失败：既无可用修订也无初稿（{reason}）")
        storage.write_article_file(dir_name, storage.FINAL, draft, base=base)
        _mark_final(dir_name, degraded=True)      # 未过审 → 标 reviewed，不进自动发布
        source = "draft-fallback"
        stages.append({"dir": dir_name, "status": "final",
                       "verdict": review.get("verdict"), "score": score,
                       "source": source})
    log.error("article_degraded_delivered", extra={
        "event": "article_degraded_delivered", "dir": dir_name,
        "score": score, "source": source, "reason": reason[:200]})
    # 降级交付**不复评**：磁盘上若还留着上一版的 04_final_review.json，档案页会
    # 显示"上一版定稿的分数"，与现在的 03_final.html 完全不是同一篇。清掉即可
    # （页面据此显示"未复评"，而不是一个错分数）。
    try:
        storage.remove_article_files(dir_name, storage.FINAL_REVIEW, base=base)
    except Exception:  # noqa: BLE001 - 清理失败不影响交付
        pass
    _notify(notify, f"✅ 已降级交付（{source}）：文章可用，但**未通过审核**，建议人工过一遍")
    return {"dir": dir_name, "stages": stages, "rounds": 0,
            "rewrites": 0, "degraded": True, "degrade_reason": reason}


def _revert_to_draft(dir_name: str, base) -> str:
    """定稿复评不合格（<60）→ 回退用初稿交付：改坏的版本不如不改。

    返回实际动作字符串，由调用方写进日志 —— 这个动作会**直接替换已落盘的
    03_final.html**，属于静默数据变更，不能不留痕。

    三条硬约束：
    1. 已发布（status=published）的不覆盖：磁盘上那份可能已经分发出去，悄悄换成
       本地初稿会让"本地档案"与"已发布内容"永久不一致（发布记录写在 PublishLog，
       不会跟着回滚）；
    2. 读不到初稿就维持现状：此刻 03_final.html 本身是好的，回退失败不该把一次
       **成功**的流程变成失败（旧实现裸调 read，异常会直接穿透整个任务）；
    3. 一切失败返回状态而不抛异常 —— 调用点在成功返回路径上，外面没有兜底。
    """
    from app.models import Article
    from app.tenancy import TenantScopedRepository

    try:
        row = TenantScopedRepository(Article).first(dir=dir_name)
    except Exception as exc:  # noqa: BLE001 - 查库失败只放弃回退，不阻断交付
        log.error("article_revert_lookup_failed", extra={
            "event": "article_revert_lookup_failed", "dir": dir_name,
            "err": f"{type(exc).__name__}: {str(exc)[:160]}"})
        return "error-lookup"
    if row is not None and row.status == "published":
        return "skipped-published"
    try:
        draft = storage.read_article_file(dir_name, storage.DRAFT, base=base)
    except Exception as exc:  # noqa: BLE001 - 没有初稿可回退，维持现定稿
        log.warning("article_revert_no_draft", extra={
            "event": "article_revert_no_draft", "dir": dir_name,
            "err": f"{type(exc).__name__}: {str(exc)[:160]}"})
        return "skipped-no-draft"
    try:
        storage.write_article_file(dir_name, storage.FINAL, draft, base=base)
    except Exception as exc:  # noqa: BLE001 - 写失败则定稿保持原样（仍是可用文章）
        log.error("article_revert_write_failed", extra={
            "event": "article_revert_write_failed", "dir": dir_name,
            "err": f"{type(exc).__name__}: {str(exc)[:160]}"})
        return "error-write"
    return "reverted"


def _snapshot_for_rewrite(dir_name: str, base) -> dict:
    """把重写轮即将删掉的资产读进内存（初稿 + 审稿）。

    重写轮**必须**删掉上一轮产物，否则 A 段按"初稿存在即跳过"的断点续跑规则
    直接返回 skipped，重写根本不会发生。但删掉之后、新初稿写出来之前，磁盘上
    这两份是**唯一**副本 —— 而 `generate_article` 里任何失败（空正文、fake
    供应商守卫、品牌校验、网络）都会让文章目录变成空的，文章凭空消失。
    """
    snap: dict = {}
    for name in (storage.DRAFT, storage.REVIEW):
        try:
            snap[name] = storage.read_article_file(dir_name, name, base=base)
        except Exception:  # noqa: BLE001 - 缺哪份算哪份，能救多少救多少
            continue
    return snap


def _restore_snapshot(dir_name: str, snap: dict, base) -> None:
    """把 `_snapshot_for_rewrite` 的备份写回（重写失败时用）。"""
    for name, text in snap.items():
        try:
            storage.write_article_file(dir_name, name, text, base=base)
        except Exception as exc:  # noqa: BLE001 - 恢复失败不掩盖原始错误，只记账
            log.error("article_rollback_failed", extra={
                "event": "article_rollback_failed", "dir": dir_name,
                "file": name, "err": f"{type(exc).__name__}: {str(exc)[:160]}"})


