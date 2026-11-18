"""抖音图文的**可发账号池**：本地绑定行 + 蚁小二云账号合并成一份清单。

从 `blueprints/generation/douyin_service.py` 迁出（A3 单文件 300 行预算；该文件
正好卡在 300 行，而这次"一包对多号"的分发口径还要往里加东西）。

页面与发布计划都读这一份：`douyin_publish.plan` 只对**这里返回的、status=1 的号**
派单，所以"哪些号能发"只能有一个答案 —— 两边各写一套的话，页面勾得上的号可能
不是真正会发的号。
"""
from __future__ import annotations

from app.models import Account
from app.services import douyin_config
from app.services.publish import yxer
from app.tenancy import TenantScopedRepository


# 预览用的云账号短缓存。只缓存**云侧**结果（一个安装只有一份蚁小二 Key），
# 本地行、额度、账本一律实时读 —— 缓存的每一秒都只影响"页面上的预估条数"。
_POOL_TTL = 8.0
_pool_cache: dict = {"key": None, "at": 0.0, "value": None}


def clear_cache() -> None:
    _pool_cache["key"] = None


def _cloud_cached() -> dict:
    import time

    conf = yxer.yxer_conf()
    key = (str(conf.get("token") or ""), bool(conf.get("use_mock")))
    if _pool_cache["key"] == key and time.monotonic() - _pool_cache["at"] < _POOL_TTL:
        return _pool_cache["value"]
    value = yxer.list_accounts()
    _pool_cache.update({"key": key, "at": time.monotonic(), "value": value})
    return value


def list_accounts(cached: bool = False) -> list:
    """可发的抖音号：本地绑定行 + 蚁小二云账号（去重合并）。

    每条都带额度字段（`daily_limit` / `remaining`），发布计划据此跳过当日打满的号。

    `cached=True` 只该被**预览**用（发布弹窗里每勾一个号就要算一次条数，而读云账号
    是起一次蚁小二 CLI 子进程，实测 0.4–1.0s/次 —— 勾 12 个号要等十几秒）。真正发布
    与页面上的账号列表永远读实时值：条数可以估算，派单不能。
    """
    rows = TenantScopedRepository(Account).query().all()
    cloud = _cloud_cached() if cached else yxer.list_accounts()
    cloud_accounts = cloud.get("accounts") or []
    cloud_ids = {str(a.get("id")) for a in cloud_accounts}
    # 换蚁小二 API Key = 换账号库：旧 Key 的账号在当前云库里查不到 id，
    # 留着只会在发布时被"账号不存在/未设代理"打回。云列表可用时按 id 白名单过滤，
    # 旧号自动不进发布池（不删库，保留历史发布关联）。
    cloud_ok = bool(cloud.get("configured")) and not cloud.get("error")
    out, seen = [], set()
    for a in rows:
        if not str(a.platform).startswith("douyin"):
            continue
        if a.yxer_id and cloud_ok and str(a.yxer_id) not in cloud_ids:
            continue                      # 旧 Key 的云账号：当前库无此 id → 不进池
        out.append({"id": a.id, "name": a.name, **quota_fields(a)})
        seen.add(a.id)
    for a in cloud_accounts:
        if not str(a.get("platform") or "").startswith("douyin"):
            continue
        local_id = f"yx-{a['id']}"
        if local_id in seen:
            continue                      # 已由本地绑定行加入，避免重复
        row = next((r for r in rows if r.yxer_id == a["id"]), None)
        if row is None and not cloud.get("mock"):
            continue                      # 未同步勾选启用的云账号不出现在发布池
        out.append({"id": local_id, "name": a["name"],
                    "status": 1 if a.get("online") else 0,
                    "frozen": False, "cloud": True,
                    **quota_fields(row, online=bool(a.get("online")))})
        seen.add(local_id)
    return out


def quota_fields(row: Account | None, online: bool | None = None) -> dict:
    """账号的额度/状态字段：`daily_limit` + `remaining`。

    `daily_limit = 0` 是**不限量**（`quota_update_sql` 的语义），此时 `remaining`
    给 `None` 而非 `0 - used` 的负数 —— 负数会被发布编排当成"当日打满"踢出池子。
    云账号（`online` 非 None）还要看蚁小二侧的在线态。
    """
    limit = int(getattr(row, "daily_limit", 0) or 0) if row is not None else 0
    used = douyin_config.used_today(row) if row is not None else 0
    ok = bool(row.enabled and row.status == "active") if row is not None else True
    if online is not None:
        ok = ok and bool(online)
    return {"status": 1 if ok else 0,
            "frozen": bool(row is not None and row.status == "frozen"),
            "daily_limit": limit,
            "remaining": None if limit <= 0 else max(0, limit - used)}