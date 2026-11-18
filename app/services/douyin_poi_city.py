"""按城市采集定位（城市名兜底 + 按区县归类）。

背景：`yxer query locations` 只按区县名搜，有时会漏掉该区——实测无锡滨湖区：
区县名翻 4 页 0 条，但搜「滨湖区」/「无锡滨湖区」/「无锡市」却能命中 5~42 条
（不同号的命中还不一样）。所以每个城市同时搜「城市名 + 各区县名」，
再把所有结果按**地址里的区县**归类、去重，取并集。
"""
from __future__ import annotations

from app.logging import get_logger
from app.services.douyin_cities import CITY_DISTRICTS
from app.services.douyin_poi_build import build_pool

log = get_logger(__name__)


def build_city_pool(account_id: str, city: str, max_pages: int = 3) -> dict:
    """采集一个城市的全部区县；返回 `{区县: [poi]}`（按地址区县归类去重）。"""
    districts = CITY_DISTRICTS.get(city) or []
    if not districts:
        return {}
    # 城市名也搜一遍：它的结果里含各区县的 POI，正好补区县名搜不到的区
    pool = build_pool([city] + districts, yxer_account_id=account_id,
                      max_pages=max_pages)
    got: dict[str, list] = {}
    seen: set[str] = set()
    for pois in (pool.get("regions") or {}).values():
        for p in pois:
            pid = str(p.get("id"))
            if pid in seen:
                continue
            dist = str((p.get("raw") or {}).get("address_info",
                                                {}).get("district") or "")
            hit = next((d for d in districts if d in dist), None)
            if hit:
                seen.add(pid)
                got.setdefault(hit, []).append(p)
    return got
