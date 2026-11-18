"""抖音图文的「定位（POI）」与「配乐（music）」解析（从 yxer_imgtext.py 拆出）。

两者都是 contentPublishForm 的复杂字段，都靠 `yxer query ...` 拿对象：
- location：`yxer query locations <account_id> --query <区县> --json`
- music：   `yxer query music <account_id> --json`（不传关键词 = 抖音推荐榜）

查不到 / 报错一律降级为「不加该字段照常发」，绝不拖累发布。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from app.logging import get_logger
from app.services.publish.drivers import PublishError
from app.services.publish.yxer_cli import run_yxer

log = get_logger(__name__)


# ── 定位（POI）解析 ───────────────────────────────────────────────────────────
# 缓存：<区县名> → POI 对象（或 None=查无）。落盘避免每条发布多一次查询，也保证
# 同一区县反复复用同一定位。查询失败不缓存（下次重试）；仅「真查无结果」才缓存 None。

_POI_CACHE_FILE = Path("data/local/douyin/poi_cache.json")
_POI_CACHE: dict = {}


def _poi_cache_load() -> None:
    global _POI_CACHE
    try:
        if _POI_CACHE_FILE.exists():
            _POI_CACHE = json.loads(_POI_CACHE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        _POI_CACHE = {}


def _poi_cache_save() -> None:
    try:
        _POI_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _POI_CACHE_FILE.write_text(json.dumps(_POI_CACHE, ensure_ascii=False),
                                   encoding="utf-8")
    except Exception:
        pass


def _extract_location_list(data: dict) -> list:
    """取 locations 候选列表（实测真实结构是 `data.dataList`，兼容其它外层键）。"""
    d = (data or {}).get("data")
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for key in ("dataList", "list", "items", "results", "data"):
            v = d.get(key)
            if isinstance(v, list):
                return v
    return []


def _coerce_location(obj) -> dict | None:
    """把查询返回的地点对象规整成 schema 要求的 location 结构。

    payload 需要：`location = {data: {raw, yixiaoerId, yixiaoerName}, isScp: bool}`。
    实测查询返回的候选**字段在顶层**，因此这里把这三项搬进 `data`。
    """
    if not isinstance(obj, dict):
        return None
    inner = obj.get("data")
    if isinstance(inner, dict) and ("yixiaoerId" in inner or "raw" in inner):
        out = dict(obj)
        out.setdefault("isScp", False)
        return out
    if obj.get("yixiaoerId") or obj.get("yixiaoerName"):
        data = {"yixiaoerId": obj.get("yixiaoerId"),
                "yixiaoerName": obj.get("yixiaoerName")}
        if "raw" in obj:
            data["raw"] = obj["raw"]
        return {"data": data, "isScp": False}
    return None


def _pick_location(items: list, city: str | None):
    """优先地址真在目标区县的（避免"异地同名"），没有才退回第一个。"""
    target = str(city or "").strip()
    if target:
        for it in items:
            info = ((it.get("raw") or {}).get("address_info")) or {}
            for key in ("province", "city", "district", "simple_addr", "address"):
                if target in str(info.get(key) or ""):
                    return it
    return items[0]


def _resolve_poi(yxer_account_id: str, city: str | None):
    """按区县名查抖音 POI，取第一个。返回 location 对象或 None。"""
    if not city or not yxer_account_id:
        return None
    if city in _POI_CACHE:
        return _POI_CACHE[city]
    try:
        data = run_yxer(["query", "locations", yxer_account_id,
                         "--query", city, "--json"], timeout=30)
    except PublishError as exc:
        log.warning("yxer_poi_query_failed",
                    extra={"event": "yxer_poi_query_failed",
                           "city": city, "err": exc.message})
        return None
    items = _extract_location_list(data)
    if not items:
        _POI_CACHE[city] = None          # 真查无结果：缓存 None，避免反复打空查询
        _poi_cache_save()
        return None
    loc = _coerce_location(_pick_location(items, city))
    if loc is not None:
        _POI_CACHE[city] = loc
        _poi_cache_save()
    return loc


_poi_cache_load()


# ── 配乐（music）解析 ────────────────────────────────────────────────────────
# `yxer query music <account_id> --json` 不传关键词 = 抖音推荐榜。
# 列表按账号缓存（推荐榜不常变），每次发布随机取一首，不固定。

_MUSIC_CACHE: dict[str, list] = {}


def _resolve_music(yxer_account_id: str):
    """从推荐榜随机挑一首背景音乐；返回 `{raw, yixiaoerId, yixiaoerName}` 或 None。"""
    if not yxer_account_id:
        return None
    items = _MUSIC_CACHE.get(yxer_account_id)
    if items is None:
        try:
            raw = run_yxer(["query", "music", yxer_account_id, "--json"],
                           timeout=30)
        except PublishError as exc:
            log.warning("yxer_music_query_failed", extra={
                "event": "yxer_music_query_failed", "err": exc.message[:120]})
            items = []
        else:
            data = (raw.get("data") or {}) if isinstance(raw, dict) else {}
            items = [it for it in (data.get("dataList") or [])
                     if isinstance(it, dict) and it.get("yixiaoerId")
                     and it.get("yixiaoerName")]
        _MUSIC_CACHE[yxer_account_id] = items
    if not items:
        return None
    pick = random.choice(items)
    music = {"yixiaoerId": str(pick.get("yixiaoerId")),
             "yixiaoerName": str(pick.get("yixiaoerName"))}
    if isinstance(pick.get("raw"), dict):
        music["raw"] = pick["raw"]
    return music
