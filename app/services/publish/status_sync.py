"""蚁小二发布结果回查：把"已提交"确认成"已发布 / 失败"。

## 为什么需要（2026-10-19 事故）

蚁小二是**异步发布**：`yxer publish` 立刻返回 `task_set_id`，真正的发布在云端排队执行。
原实现拿到 task_id 就记 `success=1` —— 于是"提交成功"被当成"已发布"：用户在蚁小二
后台看不到内容，系统里却是一排绿勾。

实测 8 条全绿，回查后发现百家号那条是：

    stageStatus: fail / stages: push
    errorMessage: 发布失败您所在网络环境异常，请完成验证

## 回查接口

    yxer query details <task_set_id> --json
    → {"ok":true,"action":"details","data":{"tasks":[{stageStatus, errorMessage,
        openUrl, title, platformName, stages, ...}]}}

判定：**有 openUrl 才算发布成功**；`stageStatus` 落在失败集合里算失败；其余待定
（待定不写成功，等下一轮再查 —— 宁可晚确认，也不能假报成功）。
"""
from __future__ import annotations

from datetime import timedelta

from app.logging import get_logger
from app.services.publish import yxer_cli

log = get_logger(__name__)

TASK_PREFIX = "yxer-task:"
PENDING_DAYS = 7          # 只回查最近 7 天的提交（更早的没有回查价值）
BATCH = 30                # 单次最多回查多少个（每条一个子进程，别一次打太多）
_FAIL_STAGES = frozenset({"fail", "failed", "error", "exception"})


def query_task(task_set_id: str) -> dict | None:
    """查一个 task_set_id 的任务详情；拿不到返回 None（回查不该打断发布流）。"""
    try:
        env = yxer_cli.run_yxer(["query", "details", task_set_id])
    except Exception as exc:  # noqa: BLE001 - CLI 不可用/超时/Key 失效 → 这轮跳过
        log.warning("yxer_details_failed", extra={
            "event": "yxer_details_failed", "task": task_set_id,
            "err": f"{type(exc).__name__}: {str(exc)[:160]}"})
        return None
    payload = env.get("data") if isinstance(env, dict) else None
    tasks = (payload or {}).get("tasks") or []
    first = tasks[0] if tasks and isinstance(tasks[0], dict) else None
    return first


# 平台侧"创作中心跳转"的跟踪参数：剥掉不影响定位文章，留着又长又像乱码。
# 实测头条那条：?enter_from=click_creation_center&category_name=creation_center
#                &gd_ext_json=%7B%22enter_from%22...%7D
_TRACKING_PARAMS = frozenset({
    "enter_from", "category_name", "gd_ext_json", "utm_source", "utm_medium",
    "utm_campaign", "spm",
})


def clean_open_url(url: str) -> str:
    """去掉跟踪参数，保留文章 id 等必需参数。

    **不做"规范化"猜测**（例如把 `/item/` 改写成 `/article/`）：链接必须来自平台，
    不能由我们编 —— 编出来的"漂亮链接"会 404 得更彻底。
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(kept)))


# 平台原生文章页：按各平台的 URL 规则从 publishId 推导。
# 为什么不用 openUrl：蚁小二返回的是**创作中心跳转链**（头条
# /item/<id>/?enter_from=click_creation_center）或**预览链**（百家号 ?preview=1），
# 浏览器直开 404 —— 用户在系统里点了链接看不到自己发的内容（2026-10-19 反馈）。
# 这是按平台公开规则推导，不是编造；推不出来就退回 openUrl。
_PUBLIC_URL_RULES = {
    "toutiao": "https://www.toutiao.com/article/{id}/",
    "baijiahao": "https://baijiahao.baidu.com/s?id={id}",
    "wangyi": "https://www.163.com/dy/article/{id}.html",
}

# 任务详情里的 platformName 是中文；映射到 publish_log.platform 的 key
_PLATFORM_ALIASES = {
    "头条号": "toutiao", "头条": "toutiao",
    "百家号": "baijiahao", "百家": "baijiahao",
    "网易号": "wangyi", "网易": "wangyi",
    "搜狐号": "souhu", "搜狐": "souhu",
}


def _platform_key(name: str) -> str:
    text = str(name or "").strip()
    return _PLATFORM_ALIASES.get(text) or _PLATFORM_ALIASES.get(text.lower()) or text.lower()


def public_url(platform: str, publish_id: str, open_url: str) -> str:
    """优先返回平台原生文章页；推不出来退回 openUrl（剥过跟踪参数的那个）。"""
    pid = str(publish_id or "").strip()
    if pid.startswith("news_"):            # 百家号 nid 形如 news_10335157152299485953
        pid = pid[len("news_"):]
    rule = _PUBLIC_URL_RULES.get(_platform_key(platform))
    if pid and rule:
        return rule.format(id=pid)
    return open_url


def classify(task: dict) -> tuple[str, str, str]:
    """(status, detail, url)：ok / fail / pending。"""
    url = clean_open_url(str(task.get("openUrl") or "").strip())
    url = public_url(task.get("platformName") or "", task.get("publishId")
                     or task.get("documentId") or "", url)
    stage = str(task.get("stageStatus") or "").strip().lower()
    err = str(task.get("errorMessage") or "").strip()
    stages = str(task.get("stages") or "").strip()
    # 失败优先：有的失败任务也会带 openUrl（历史记录/草稿地址），不能让 URL 把失败盖成成功
    if stage in _FAIL_STAGES:
        return "fail", (err or f"发布失败（阶段：{stages or '未知'}）"), ""
    # 实测成功样本：stageStatus="success" 且带 publishId + openUrl。
    # 注意 openUrl 是**创作中心跳转链**（尾巴挂 enter_from/gd_ext_json），
    # 浏览器直开可能 404 —— 它证明"平台侧有这篇文章"，但不保证公开可访问。
    if stage == "success" or url:
        return "ok", "", url
    return "pending", err, ""


_LAST: dict[str, float] = {}          # 租户 → 上次回查时间戳（节流用）


def sync_if_due(tid: str, every_sec: float = 300.0) -> dict:
    """按租户节流回查（每次回查要起子进程，不能每轮调度都跑）。"""
    import time

    now = time.time()
    last = _LAST.get(tid)
    if last is not None and (now - last) < every_sec:
        return {"skipped": True}
    _LAST[tid] = now
    try:
        counts = sync_pending()
        if counts.get("checked"):
            log.info("publish_sync_tick", extra={"event": "publish_sync_tick", **counts})
        return counts
    except Exception:  # noqa: BLE001 - 回查失败不影响调度主流程
        log.warning("publish_sync_tick_failed", exc_info=True)
        return {"error": True}


def sync_pending(limit: int = BATCH) -> dict:
    """回查最近提交且未确认的发布日志，更新 remote_status / remote_*。

    失败的那条会把 `success` 摘成 False 并写原因 —— 页面上的"成功"必须与真实一致。
    """
    from sqlalchemy import or_

    from app.extensions import db
    from app.models import PublishLog
    from app.tenancy import TenantScopedRepository
    from app.timeutil import now_local

    cutoff = now_local() - timedelta(days=PENDING_DAYS)
    rows = (TenantScopedRepository(PublishLog).query()
            .filter(PublishLog.url.like(f"{TASK_PREFIX}%"))
            .filter(PublishLog.happened_at >= cutoff)
            .filter(or_(PublishLog.remote_status.is_(None),
                        PublishLog.remote_status == "pending"))
            .order_by(PublishLog.id.desc())
            .limit(limit).all())

    counts = {"checked": 0, "ok": 0, "fail": 0, "pending": 0, "unknown": 0}
    for row in rows:
        task = query_task(str(row.url)[len(TASK_PREFIX):])
        if task is None:
            counts["unknown"] += 1
            continue
        status, detail, url = classify(task)
        row.remote_status = status
        row.remote_detail = detail or None
        row.remote_url = url or None
        row.checked_at = now_local()
        if status == "fail":
            # 提交≠发布：失败必须摘掉绿勾，否则用户在系统里看到成功、平台上什么都没有
            row.success = False
            row.error = detail or "发布失败（蚁小二）"
        # **不覆盖 row.url** —— 它存的是 yxer-task:<id>，是下次回查这条任务的凭据。
        # 真实链接写在 remote_url，由发布记录接口优先返回（以前直接把 url 改成链接，
        # 任务 ID 就丢了，回查再也找不到那条任务）。
        counts[status] += 1
        counts["checked"] += 1
        log.info("publish_result_synced", extra={
            "event": "publish_result_synced", "log_id": row.id,
            "status": status, "detail": (detail or "")[:160]})
    db.session.commit()
    return counts
