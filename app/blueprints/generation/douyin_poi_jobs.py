"""抖音图文「定位池」构建任务（从 douyin_jobs.py 拆出，控制单文件 300 行门禁）。

建池是重活（198 区县 × 最多 3 页，约 5~15 分钟），必须后台跑，不能进发布主链路。
任务本体只做"调 refresh_pool + 在状态桥里如实记账"，分配算法在 services/douyin_location.py。
"""
from __future__ import annotations

from app.blueprints.generation.douyin_jobs import (
    _log, _st, poipool_begin, poipool_end)
from app.logging import get_logger
from app.platform.jobs import task
from app.tenancy import current_tenant

log = get_logger(__name__)


def ensure_pool_built(pairs, locs: dict, st: dict) -> None:
    """发布时的定位日志：分到了就报数；整批都没分到就提示去「定位管理」刷新。

    定时/循环/手动发布共用 `dy_publish_job`，都会经过这里。**不在发布链路里建池**：
    建池是 5-15 分钟的重活，会挤占生成队列 —— 由用户在页面上手动刷新。
    """
    assigned = sum(1 for v in (locs or {}).values() if v)
    if assigned:
        _log(st, "pub_logs", f"📍 定位已分配：{assigned}/{len(pairs or [])} 个账号")
    elif pairs:
        _log(st, "pub_logs", "⏭ 定位池为空/已耗尽：本次不加定位（可在「定位管理」页刷新）")


@task("douyin.poipool.build")
def dy_poipool_build_job(payload: dict) -> dict:
    """按区县抓取抖音 POI 并整份落盘。"""
    from app.services import douyin_poi_build

    tid = current_tenant()
    st = _st(tid)
    poipool_begin(tid)
    _log(st, "pub_logs", "⏳ 开始刷新定位池（按区县抓取抖音 POI，约 5-15 分钟）…")
    try:
        res = douyin_poi_build.refresh_pool(
            max_pages=int(payload.get("max_pages") or 3))
    except Exception as exc:  # noqa: BLE001 - 失败如实记账，不拖垮服务
        log.warning("douyin_poipool_failed", extra={
            "event": "douyin_poipool_failed", "err": str(exc)[:200]})
        _log(st, "pub_logs", f"❌ 定位池刷新失败：{str(exc)[:80]}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        poipool_end(tid)
    if res.get("ok"):
        _log(st, "pub_logs",
             f"✅ 定位池刷新完成：{res.get('regions')} 个地区 / {res.get('pois')} 个地址")
    else:
        _log(st, "pub_logs", f"❌ 定位池刷新失败：{res.get('error')}")
    return res
