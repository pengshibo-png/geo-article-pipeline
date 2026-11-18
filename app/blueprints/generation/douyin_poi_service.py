"""抖音定位池的查询 / 刷新（从 douyin_service.py 拆出，控制单文件 300 行门禁）。

建池任务在 douyin_poi_jobs.py；分配算法在 services/douyin_location.py。
"""
from __future__ import annotations

from app.blueprints.generation import douyin_jobs
from app.tenancy import current_tenant


def poi_pool_status() -> dict:
    from app.services import douyin_store

    pool = douyin_store.load_poi_pool()
    state = douyin_store.load_loc_state()
    regions = pool.get("regions") or {}
    snap = douyin_jobs.snapshot(current_tenant())
    return {"ok": True,
            "building": bool((snap.get("poipool") or {}).get("running")),
            "built_at": (pool.get("meta") or {}).get("built_at", ""),
            "regions": len(regions),
            "pois": sum(len(v) for v in regions.values()),
            "sample": sorted(regions)[:10],
            "accounts_with_history": len(state.get("accounts") or {})}


def poi_pool_detail() -> dict:
    """定位池 + 分配状态的完整视图（页面「定位管理」用）。"""
    from app.models import Account
    from app.services import douyin_store
    from app.tenancy import TenantScopedRepository

    pool = douyin_store.load_poi_pool()
    state = douyin_store.load_loc_state()
    regions = pool.get("regions") or {}
    used_map = {r: {str(x) for x in ((e or {}).get("used") or [])}
                for r, e in (state.get("region_used") or {}).items()}
    region_list = []
    for name in sorted(regions):
        pois = regions[name]
        used = used_map.get(name, set())
        region_list.append({
            "name": name,
            "total": len(pois),
            "used": sum(1 for p in pois if str(p.get("id")) in used),
            "pois": [{"id": str(p.get("id")), "name": str(p.get("name") or ""),
                      "used": str(p.get("id")) in used} for p in pois],
        })
    names = {a.id: (a.name or a.id)
             for a in TenantScopedRepository(Account).query().all()}
    accounts = [{"id": aid,
                 "name": names.get(aid) or aid,
                 "regions": list((ent or {}).get("regions") or [])}
                for aid, ent in (state.get("accounts") or {}).items()]
    snap = douyin_jobs.snapshot(current_tenant())
    # 未取到的区县：failed=接口失败，empty=接口正常但当地没有 POI。
    # all_regions 优先用建池时存下的；旧池没有就回落到当前业务城市（立即可用）。
    from app.services import douyin_poi_build

    all_regions = (pool.get("meta") or {}).get("all_regions") or douyin_poi_build.city_regions()
    failed = set(pool.get("failed") or [])
    ok = set(regions)
    unavailable = [{"name": r, "state": "failed" if r in failed else "empty"}
                   for r in all_regions if r not in ok]
    return {"ok": True,
            "building": bool((snap.get("poipool") or {}).get("running")),
            "built_at": (pool.get("meta") or {}).get("built_at", ""),
            "regions": region_list,
            "accounts": accounts,
            "unavailable": unavailable,
            "skipped": pool.get("skipped") or []}


def poi_preview(body: dict) -> dict:
    """发布前的定位预览（**不落盘**）：每对 (pack, account) 的自动/预设/手选结果。"""
    from copy import deepcopy

    from app.blueprints.generation.douyin_service import DouyinService
    from app.services import douyin_location as dl, douyin_publish, douyin_store

    pack_ids = [str(x) for x in (body.get("pack_ids") or [])]
    account_ids = [str(x) for x in (body.get("account_ids") or [])]
    mode = douyin_publish.resolve_mode(body.get("mode"))
    pairs, _notes = douyin_publish.plan(pack_ids, account_ids,
                                        DouyinService().accounts(), mode=mode)
    pool = douyin_store.load_poi_pool()
    raw_state = douyin_store.load_loc_state()
    presets = raw_state.get("overrides") or {}
    picks = dl.overrides_from_payload(body.get("locations"))
    out = dl.allocate(pairs, pool, deepcopy(raw_state), overrides=picks)
    index = {str(p.get("id")): (r, p.get("name"))
             for r, pois in (pool.get("regions") or {}).items() for p in pois}
    items = []
    for (pid, aid), loc in out.items():
        if not loc:
            items.append({"pack_id": pid, "account_id": aid, "poi_id": "",
                          "region": "", "name": "", "source": "none"})
            continue
        poi_id = str(loc["data"]["yixiaoerId"])
        region, name = index.get(poi_id, ("", ""))
        src = "manual" if (pid, aid) in picks else (
            "preset" if (presets.get(pid) or {}).get(aid) else "auto")
        items.append({"pack_id": pid, "account_id": aid, "poi_id": poi_id,
                      "region": region, "name": name or "", "source": src})
    return {"ok": True, "items": items, "mode": mode}


def poi_override_list(pack_id: str) -> dict:
    from app.services import douyin_store

    state = douyin_store.load_loc_state()
    ov = (state.get("overrides") or {}).get(str(pack_id or "")) or {}
    return {"ok": True, "overrides": ov}


def poi_override_set(body: dict) -> dict:
    """设置/清除某个 (作品, 账号) 的定位预设；`poi_id` 空 = 恢复自动。"""
    from app.services import douyin_store

    pack_id = str(body.get("pack_id") or "")
    account_id = str(body.get("account_id") or "")
    poi_id = str(body.get("poi_id") or "")
    if not pack_id or not account_id:
        return {"ok": False, "error": "pack_id 与 account_id 必填"}
    with douyin_store.loc_state_lock():
        state = douyin_store.load_loc_state()
        ov = state.setdefault("overrides", {})
        if poi_id:
            ov.setdefault(pack_id, {})[account_id] = poi_id
        else:
            ov.get(pack_id, {}).pop(account_id, None)
            if not ov.get(pack_id):
                ov.pop(pack_id, None)
        douyin_store.save_loc_state(state)
    return {"ok": True, "pack_id": pack_id, "account_id": account_id, "poi_id": poi_id}


def poi_pool_refresh(body: dict) -> tuple[dict, int]:
    from app.platform import jobs

    snap = douyin_jobs.snapshot(current_tenant())
    if (snap.get("poipool") or {}).get("running"):
        return ({"ok": False, "error": "定位池正在刷新中，请稍候"}, 409)
    max_pages = max(1, min(int(body.get("max_pages") or 3), 5))
    douyin_jobs.poipool_begin(current_tenant())
    jobs.enqueue("douyin.poipool.build", {"max_pages": max_pages},
                 tenant_id=current_tenant(), queue="gen")
    return ({"ok": True, "queued": True, "max_pages": max_pages}, 200)
