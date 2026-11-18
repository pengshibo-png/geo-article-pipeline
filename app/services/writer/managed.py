"""受管生成：页面驱动的选题/文章批量任务（任务体 + 批量启动入口）。

状态桥（进度/日志/成败计数）在 `app/services/writer/run_bridge.py`
（A3 行数预算拆分）；本模块 re-export，调用点（api/scheduler/测试）不变。

- 两个任务名都是新注册（`titles.generate.managed` / `article.pipeline.managed`），
  `task()` 重复注册会 ValueError，不复用 pipeline 里的旧任务名。
- `begin_run` 是 /api/run 与调度线程共用的批量启动入口：品牌预检 → 取题
  标 used（池空自动补货，§1.5 规则 9）→ 状态桥置位 → 逐条入队。
"""
from __future__ import annotations

import traceback

from app.logging import get_logger
from app.platform import jobs
from app.platform.jobs import task
from app.tenancy import TenantScopedRepository, current_tenant
from app.services.writer import title_context
from app.services.writer.run_bridge import (  # noqa: F401  re-export：调用点零改动
    current_tid, gen_abort, gen_begin, gen_finish_article, gen_log,
    gen_request_pause, gen_running, gen_set_total, gen_should_skip, gen_snapshot,
    gen_start_article, gen_try_begin, pause_run,
    reset_states, titles_begin, titles_finish, titles_log, titles_record_error,
    titles_request_stop, titles_running, titles_set_produced, titles_set_target,
    titles_snapshot, titles_stop_requested,
)

log = get_logger(__name__)

_CONTACT_MODES = ("revise", "strip", "keep")


# ── 批量写稿启动（HTTP /api/run 与调度线程共用；需已绑定租户上下文）─────────

def brand_block() -> str | None:
    """品牌预检（与 A 段 R2 阻塞同口径）：品牌不可用就别烧选题。"""
    from app.models import Brand
    from app.services.writer.derived import derive_brand_names

    row = TenantScopedRepository(Brand).first()
    if not row or not (row.content or "").strip():
        return "请先到「品牌资料」页填写品牌资料（AI 写稿需要引用您的品牌信息）"
    if not derive_brand_names(row.content)["ok"]:
        return "品牌资料「品牌概述」首句识别不出品牌名，请按「正式名称，是……」格式填写"
    return None


def begin_run(count: int, contact_mode: str = "revise") -> dict:
    """取题 → 标 used → 状态桥置位 → 逐条入队 article.pipeline.managed。

    池空时自动跑 T 段补货（施工规格 §1.5 规则 9；≤3 轮）。
    返回 {ok, error?} 或 {ok:true, started:N, replenished:N}。
    """
    from app.models import Title

    tid = current_tenant()
    if gen_running(tid):
        return {"ok": False, "error": "已有写稿任务在运行，请等待完成或先暂停"}
    blocked = brand_block()
    if blocked:
        return {"ok": False, "error": blocked}
    count = max(1, min(int(count or 1), 200))
    if contact_mode not in _CONTACT_MODES:
        contact_mode = "revise"

    # ★ 原子抢占：下面取题/补货（补货同步调 LLM，可能几十秒）与"是否在跑"之间
    # 原本没有互斥 —— 用户连点或前端超时重试会让两个请求拿到同一批选题、双份计费、
    # 并发生成互写同一目录。抢占失败就说明确实有批次在跑。
    if not gen_try_begin(tid):
        return {"ok": False, "error": "已有写稿任务在运行，请等待完成或先暂停"}
    try:
        repo = TenantScopedRepository(Title)
        rows = _fresh_titles(repo, count)
        replenished = 0
        if not rows:
            replenished = _replenish_titles(repo, count)
            rows = _fresh_titles(repo, count)
        if not rows:
            gen_abort(tid, "选题库没有可用标题")
            return {"ok": False,
                    "error": "选题库没有可用标题：请先到「选题库」生成或添加选题"}
        for r in rows:
            r.used = True
        gen_set_total(tid, len(rows))       # 抢占时还不知道条数，取完题回写
        if replenished:      # 补货日志在 begin 之后（begin 会清空 log_tail）
            gen_log(tid, f"标题池为空，已自动补货 {replenished} 条选题")
        gen_log(tid, f"任务启动：共 {len(rows)} 篇 · 联系方式模式 {contact_mode}")
        from app.extensions import db
        db.session.commit()  # 先落 used 标记再入队（任务体从 payload 拿标题）
        for r in rows:
            # ★ 年份兜底：**批量写稿走的是 job payload 传 title**（不是
            # `generate_article` 的 `_take_title()` 分支），所以必须在这里补。
            # 存量选题建库时都没有年份（实测 94 条全无），GEO 抓不到时效信号。
            jobs.enqueue("article.pipeline.managed",
                         {"title": title_context.normalize_title(r.text),
                          "title_id": r.id, "contact_mode": contact_mode},
                         tenant_id=tid, queue="gen")
    except Exception as exc:  # noqa: BLE001 - 启动失败必须复位，否则永久卡死
        gen_abort(tid, f"启动失败：{type(exc).__name__}: {exc}")
        raise
    return {"ok": True, "started": len(rows), "replenished": replenished}


def _fresh_titles(repo, count: int) -> list:
    from app.models import Title

    return (repo.query().filter(Title.used.is_(False), Title.deleted_at.is_(None))
            .order_by(Title.created_at.asc(), Title.id.asc()).limit(count).all())


def _replenish_titles(repo, count: int) -> int:
    """标题池补货循环（§1.5 规则 9）；每轮 commit 让新题对下一轮可见。"""
    from app.models import Title
    from app.services.writer.pipeline import generate_titles
    from app.extensions import db

    added = 0
    for _ in range(3):
        have = repo.query().filter(Title.used.is_(False), Title.deleted_at.is_(None)).count()
        if have >= count:
            break
        got = generate_titles(max(count - have, 1))
        db.session.commit()
        if not got:
            break
        added += len(got)
    return added


# ── 任务体（在 worker 线程里执行，租户由 run_job 绑定）──────────────────────

def _release_title(title_id) -> None:
    """暂停跳过时把选题退回（used 复位），对应 gen.html 的「选题保留，随时可续跑」。"""
    from app.models import Title

    if not title_id:
        return
    row = TenantScopedRepository(Title).query().filter(Title.id == title_id).first()
    if row is not None:
        row.used = False
        from app.extensions import db

        db.session.commit()      # 即写即交：写锁不跨任务


@task("article.pipeline.managed")
def article_pipeline_managed(payload: dict) -> dict:
    """单篇 A→B→C + 进度回报。失败会计入 fail 并继续跑完整个批次。"""
    tid = current_tenant()
    title = str(payload.get("title") or "")
    if gen_should_skip(tid):
        _release_title(payload.get("title_id"))
        gen_log(tid, f"⏸ 「{title}」已跳过（收到暂停指令，选题已退回）")
        gen_finish_article(tid, title, ok=None)
        return {"status": "canceled"}
    gen_start_article(tid, title)
    gen_log(tid, f"▶ 开始生成「{title}」")
    from app.services.writer.rewrite import run_with_rewrite

    try:
        # notify → 页面日志：重写环会实时打出「↺ 审稿判定建议重写 → 回 A 段重写（第 2 轮）」
        out = run_with_rewrite(payload, notify=lambda msg: gen_log(tid, msg))
        rewrites = int(out.get("rewrites") or 0)
        if out.get("degraded"):
            # 降级交付：文章可用但**没过审** —— 必须在页面上看得见，
            # 否则等于把未过审的稿子悄悄混进成品里。
            gen_log(tid, f"⚠ 完成「{title}」（降级交付：未通过审核，建议人工过一遍）")
        else:
            gen_log(tid, f"✅ 完成「{title}」"
                         + (f"（自动重写 {rewrites} 轮后通过审核）" if rewrites else ""))
        if out.get("final_score") is not None:
            gen_log(tid, f"   定稿复评 {out['final_score']} 分（{out.get('final_verdict')}）"
                         + ("；复评不合格，已回退初稿"
                            if out.get("final_reverted") else ""))
        gen_finish_article(tid, title, ok=True)
        return out
    except Exception as exc:
        # 失败必须落**磁盘**日志：状态桥在内存里，进程一重启失败原因就永久丢失
        # —— 用户只会看到"少生成了几篇"，再也没法归因（19 篇失败零痕迹即如此）。
        log.error("article_pipeline_failed", extra={
            "event": "article_pipeline_failed", "title": title,
            "err": f"{type(exc).__name__}: {str(exc)[:300]}",
            "trace": traceback.format_exc()[-600:]})
        gen_log(tid, f"❌ 失败「{title}」：{exc}")
        gen_finish_article(tid, title, ok=False)
        raise


@task("titles.generate.managed")
def titles_generate_managed(payload: dict) -> dict:
    """批量选题循环：一批一批跑到达标 / 停止 / 无新增。错误记入状态桥（页面读 error）。

    状态由 API/调度线程 titles_begin() 先行置位（防"入队后任务未起跑"的轮询竞态）。
    """
    tid = current_tenant()
    per_batch = max(1, min(int(payload.get("count", 20)), 200))
    target = max(per_batch, min(int(payload.get("target") or per_batch), 1000))
    titles_log(tid, f"开始生成选题：目标 {target} 条，每批 {per_batch} 条")
    titles_set_target(tid, target)
    from app.services.writer.pipeline import generate_titles

    produced: list = []
    try:
        while len(produced) < target:
            if titles_stop_requested(tid):
                titles_log(tid, "⏸ 收到停止指令，本批后收束")
                break
            batch = min(per_batch, target - len(produced))
            titles_log(tid, f"正在生成第 {len(produced) + 1}~{len(produced) + batch} 条…")
            got = generate_titles(batch)
            produced.extend(got)
            titles_set_produced(tid, produced)
            titles_log(tid, f"本批入库 {len(got)} 条，累计 {len(produced)}/{target}")
            if not got:
                titles_log(tid, "本轮没有新选题通过查重，提前收束")
                break
    except Exception as exc:
        log.exception("titles_generate_failed", extra={"event": "titles_generate_failed"})
        titles_record_error(tid, str(exc))
        titles_log(tid, f"❌ 生成失败：{exc}")
        return {"ok": False, "error": str(exc)}
    finally:
        titles_finish(tid)
    met = bool(produced) and len(produced) >= target
    titles_log(tid, f"✅ 选题生成结束：共 {len(produced)} 条" + ("（达标）" if met else ""))
    return {"ok": True, "titles": produced, "target": target, "target_met": met}
