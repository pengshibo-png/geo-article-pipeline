"""抖音图文「定位(POI)」分配（建池逻辑在 douyin_poi_build.py）。

用户 2026-10-19 定的三层不重复规则：
1. **账号间**：同一批发布内，每个账号拿到的 POI 各不相同（随机、不重复）；
2. **同账号前后**：同一账号本次地区 ≠ 上次地区（默认只看上一条）；
3. **同地区**：同一地区被多次使用时，每次地址不同。

口径（已与用户确认）：
- 一包多号：标题城市固定，只让定位在不同账号间变（允许标题地区 ≠ 定位地区）；
- 某地区本轮地址用尽 → 换到还有余量的地区；
- 近期循环：所有地区用满一轮后重置本轮记录，允许复用；
- 手动指定优先：发布弹窗手选 > 作品库预设 > 自动分配。
"""
from __future__ import annotations

import random

from app.logging import get_logger
from app.services.douyin_poi_filter import good_first, is_bad as _is_bad, is_target
from app.timeutil import now_local

log = get_logger(__name__)

DEFAULT_RECENT_N = 1       # "同账号前后"只看上一条；需要更严可调大


def _location_of(poi: dict) -> dict:
    """池条目 → payload 需要的 location 结构。"""
    data = {"yixiaoerId": poi.get("id"), "yixiaoerName": poi.get("name")}
    if isinstance(poi.get("raw"), dict):
        data["raw"] = poi["raw"]
    return {"data": data, "isScp": False}


def _find_poi(region_pois: dict, poi_id) -> tuple[str | None, dict | None]:
    """在池里按 poi_id 找 (地区, poi)；找不到返回 (None, None)。"""
    target = str(poi_id or "")
    if not target:
        return None, None
    for region, pois in region_pois.items():
        for p in pois:
            if str(p.get("id")) == target:
                return region, p
    return None, None


def allocate(pairs, pool: dict, state: dict, *,
             recent_n: int = DEFAULT_RECENT_N, rng=random,
             overrides: dict | None = None) -> dict:
    """纯分配函数：返回 `{(pack_id, account_id): location|None}`，并原地更新 state。

    优先级：**手动指定**（发布请求 > 作品库预设）> 自动：
    ① 地区不在该账号近期 ② 地区本轮还有未用地址 ③ 同批已用地址排除
    ④ 尽量铺开到本批还没用过的地区 ⑤ 某地区用尽换区 ⑥ 全用尽重置本轮。
    """
    region_pois = {
        str(r): [p for p in (ps or []) if isinstance(p, dict) and p.get("id")]
        for r, ps in ((pool or {}).get("regions") or {}).items()
    }
    region_pois = {r: ps for r, ps in region_pois.items() if ps}
    accounts = state.setdefault("accounts", {})
    region_used = state.setdefault("region_used", {})
    saved = state.setdefault("overrides", {})      # 作品库预设：pack → account → poi_id
    overrides = overrides or {}                     # 发布弹窗手选：优先级更高
    batch_ids: set[str] = set()
    batch_regions: set[str] = set()
    out: dict = {}
    recent_n = max(1, int(recent_n or 1))

    def used_ids(region: str) -> set[str]:
        return {str(x) for x in ((region_used.get(region) or {}).get("used") or [])}

    def available(region: str) -> list:
        taken = used_ids(region)
        return [p for p in region_pois[region]
                if str(p.get("id")) not in taken
                and str(p.get("id")) not in batch_ids
                and is_target(p.get("name"))]

    def reset_cycle() -> None:
        for r in region_pois:
            ent = region_used.setdefault(r, {"cycle": 1, "used": []})
            ent["cycle"] = int(ent.get("cycle") or 1) + 1
            ent["used"] = []

    def consume(aid: str, region: str, poi: dict) -> None:
        """记一次使用：本轮已用 + 本批已用 + 账号最近地区。"""
        poi_id = str(poi["id"])
        ent = region_used.setdefault(region, {"cycle": 1, "used": []})
        ent.setdefault("used", [])
        if poi_id not in ent["used"]:
            ent["used"].append(poi_id)
        batch_ids.add(poi_id)
        batch_regions.add(region)
        acc = accounts.setdefault(aid, {"regions": []})
        hist = list(acc.get("regions") or [])
        hist.append(region)
        acc["regions"] = hist[-recent_n:]

    # 本轮全用尽 → 开新一轮（仅在批次开始前判断；批内不重置，保证规则 1 不重复）
    if region_pois and all(not available(r) for r in region_pois):
        reset_cycle()

    for pack_id, account_id in pairs or []:
        pid, aid = str(pack_id), str(account_id)
        # 手动指定优先：发布请求里的 > 作品库预设的
        manual = overrides.get((pid, aid)) or (saved.get(pid) or {}).get(aid)
        if manual:
            m_region, m_poi = _find_poi(region_pois, manual)
            if m_poi is not None:
                consume(aid, m_region, m_poi)
                out[(pid, aid)] = _location_of(m_poi)
                continue
        recent = list((accounts.get(aid) or {}).get("regions") or [])[-recent_n:]
        cand = [r for r in region_pois if r not in recent and available(r)]
        if not cand:
            cand = [r for r in region_pois if available(r)]
        if not cand:
            out[(pid, aid)] = None
            continue
        spread = [r for r in cand if r not in batch_regions]
        region = rng.choice(spread or cand)
        # 好地点（商场/小区/商业街）排前面；学校类已在 available 里排除
        poi = rng.choice(good_first(available(region)))
        consume(aid, region, poi)
        out[(pid, aid)] = _location_of(poi)

    state["updated_at"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
    return out


def overrides_from_payload(locations) -> dict:
    """发布弹窗手选 `[{pack_id, account_id, poi_id}]` → `{(pack,account): poi_id}`。"""
    return {(str(x.get("pack_id")), str(x.get("account_id"))): str(x.get("poi_id"))
            for x in (locations or [])
            if isinstance(x, dict) and x.get("poi_id")}


def pool_empty() -> bool:
    """定位池是否为空（从未构建成功 / 构建失败）。"""
    from app.services import douyin_store

    return not ((douyin_store.load_poi_pool() or {}).get("regions") or {})


def allocate_batch(pairs, *, recent_n: int = DEFAULT_RECENT_N,
                   overrides: dict | None = None) -> dict:
    """发布前整批分配：读池/状态 → 分配 → 落盘。池空返回全 None。

    `overrides`：发布弹窗手选的 `{(pack_id, account_id): poi_id}`，优先于作品库预设。
    """
    from app.services import douyin_store

    pairs = [(str(p), str(a)) for p, a in (pairs or [])]
    if not pairs:
        return {}
    pool = douyin_store.load_poi_pool()
    if not (pool.get("regions")):
        log.warning("douyin_poi_pool_empty", extra={
            "event": "douyin_poi_pool_empty", "pairs": len(pairs)})
        return {p: None for p in pairs}
    with douyin_store.loc_state_lock():
        state = douyin_store.load_loc_state()
        out = allocate(pairs, pool, state, recent_n=recent_n, overrides=overrides)
        douyin_store.save_loc_state(state)
    log.info("douyin_poi_allocated: %d/%d",
             sum(1 for v in out.values() if v), len(pairs))
    return out
