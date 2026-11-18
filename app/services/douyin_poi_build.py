"""抖音图文「定位池」构建（从 douyin_location.py 拆出，控制 300 行门禁）。

`yxer query locations` 是**名称模糊匹配**，且按调用方所在区域限定。实测（2026-10-20）：
上海账号搜「鄞州区/德清县/宁波天一广场」，返回的全是上海的同名商家，翻 3 页 0 条当地。
所以只把「地址真在该区县」的 POI 收进池；其余区县如实记为 failed（接口失败）或
empty（当地无 POI），由页面展示"哪些区县没取到、为什么"。
"""
from __future__ import annotations

from app.logging import get_logger
from app.services.publish.drivers import PublishError
from app.services.publish.yxer_cli import run_yxer
from app.services.publish.yxer_imgtext import _extract_location_list
from app.timeutil import now_local

log = get_logger(__name__)

DEFAULT_MAX_PAGES = 3      # 每个地区最多抓 3 页（实测每页 3~4 条）


def city_regions() -> list[str]:
    """业务城市里的区/县/县级市（排除 25 个地级市名）。"""
    from app.models import TitleConfig
    from app.services.douyin_pack import _PREFECTURE_CITIES
    from app.tenancy import TenantScopedRepository

    tc = TenantScopedRepository(TitleConfig).first()
    raw = (tc.cities if tc else "") or ""
    names = [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]
    return [n for n in names if n not in _PREFECTURE_CITIES]


def _address_matches(item: dict, target: str) -> bool:
    """地址里是否真含目标区县（用于过滤异地同名公司）。"""
    info = ((item.get("raw") or {}).get("address_info")) or {}
    for key in ("province", "city", "district", "simple_addr", "address"):
        if target in str(info.get(key) or ""):
            return True
    return False


def _to_poi(item, region: str) -> dict | None:
    """把查询结果转成池条目；地址不在目标区县的丢弃。"""
    if not isinstance(item, dict):
        return None
    poi_id = str(item.get("yixiaoerId") or "").strip()
    name = str(item.get("yixiaoerName") or "").strip()
    if not poi_id or not name or not _address_matches(item, region):
        return None
    from app.services.douyin_poi_filter import is_bad, is_target_poi
    if is_bad(name):
        return None                      # 学校/培训/政府/医院等不进池
    entry = {"id": poi_id, "name": name,
             "raw": item.get("raw") if isinstance(item.get("raw"), dict) else {},
             "desc": str(item.get("yixiaoerDesc") or "")}
    # 按抖音官方分类判断（poi_frontend_type 优先，无分类回退名称白名单）
    if not is_target_poi(entry):
        return None                      # 只收「商场 / 中高端小区」
    return entry


# 默认关键词模板：只搜区县名。实测很多商场名里不含区名（如「武清福源万达广场」
# 不含「武清区」），单关键词覆盖率只有 50% 左右，所以支持多关键词叠加。
DEFAULT_QUERIES = ("{r}",)


def build_pool(regions, *, yxer_account_id: str,
               max_pages: int = DEFAULT_MAX_PAGES,
               queries=None) -> dict:
    """逐地区翻页抓取；返回 regions / failed / meta（纯函数，落盘见 refresh_pool）。

    `queries`：关键词模板列表，`{r}` 会替换成区县名。默认 `("{r}",)` 保持旧行为；
    传 `("{r}", "{r}商场", "{r}小区")` 可显著提升覆盖率（结果自动按 poi id 去重）。
    """
    out: dict[str, list] = {}
    failed: list[str] = []
    templates = tuple(queries or DEFAULT_QUERIES)
    regions = list(regions or [])
    for idx, raw_region in enumerate(regions, 1):
        if idx % 10 == 0:      # 建池是长任务：每 10 个地区落一条进度，便于排查卡点
            log.warning("douyin_poi_pool_progress", extra={
                "event": "douyin_poi_pool_progress", "done": idx,
                "total": len(regions), "regions_with_pois": len(out)})
        region = str(raw_region or "").strip()
        if not region:
            continue
        pois: list[dict] = []
        seen: set[str] = set()
        for tpl in templates:
            _collect_region(region, str(tpl).replace("{r}", region), yxer_account_id,
                            max_pages, pois, seen, failed)
        if pois:
            out[region] = pois
    return {"regions": out, "failed": failed,
            "meta": {"built_at": now_local().strftime("%Y-%m-%d %H:%M"),
                     "regions": len(out),
                     "pois": sum(len(v) for v in out.values()),
                     "total_regions": len(regions)}}


def _collect_region(region: str, query: str, yxer_account_id: str, max_pages: int,
                    pois: list, seen: set, failed: list) -> None:
    """用单个 query 翻页抓取，结果并入 `pois`（按 poi id 去重）。

    `region` 用于地址校验（`_to_poi` 只收地址真在该区县的 POI），
    `query` 是实际发给搜索的关键词（可带「商场」「小区」等后缀）。
    """
    page = None
    for _ in range(max(1, int(max_pages or 1))):
        cmd = ["query", "locations", yxer_account_id,
               "--query", query, "--json"]
        if page is not None:
            cmd += ["--next-page", str(page)]
        try:
            raw = run_yxer(cmd, timeout=60)
        except PublishError as exc:
            failed.append(region)
            log.warning("douyin_poi_query_failed", extra={
                "event": "douyin_poi_query_failed", "region": region,
                "query": query, "err": exc.message[:120]})
            break
        except Exception as exc:  # noqa: BLE001 - 单地区失败不断整池
            failed.append(region)
            log.warning("douyin_poi_query_error", extra={
                "event": "douyin_poi_query_error", "region": region,
                "query": query, "err": str(exc)[:120]})
            break
        items = _extract_location_list(raw)
        if not items:
            break
        for it in items:
            poi = _to_poi(it, region)
            if poi and poi["id"] not in seen:
                seen.add(poi["id"])
                pois.append(poi)
        page = (raw.get("data") or {}).get("nextPage")
        if not page:
            break


# 江浙沪 25 个地级市 → 区县（按账号代理城市分组建池）
CITY_DISTRICTS = {
    "上海市": ["黄浦区", "徐汇区", "长宁区", "静安区", "普陀区", "虹口区", "杨浦区",
            "闵行区", "宝山区", "嘉定区", "浦东新区", "金山区", "松江区", "青浦区",
            "奉贤区", "崇明区"],
    "南京市": ["玄武区", "秦淮区", "建邺区", "鼓楼区", "栖霞区", "雨花台区", "江宁区",
            "浦口区", "六合区", "溧水区", "高淳区"],
    "无锡市": ["梁溪区", "锡山区", "惠山区", "滨湖区", "新吴区", "江阴市", "宜兴市"],
    "徐州市": ["云龙区", "鼓楼区", "贾汪区", "泉山区", "铜山区", "丰县", "沛县",
            "睢宁县", "新沂市", "邳州市"],
    "常州市": ["天宁区", "钟楼区", "新北区", "武进区", "金坛区", "溧阳市"],
    "苏州市": ["姑苏区", "虎丘区", "吴中区", "相城区", "吴江区", "常熟市",
            "张家港市", "昆山市", "太仓市"],
    "南通市": ["崇川区", "通州区", "海门区", "如东县", "启东市", "如皋市", "海安市"],
    "连云港市": ["连云区", "海州区", "赣榆区", "东海县", "灌云县", "灌南县"],
    "淮安市": ["清江浦区", "淮安区", "淮阴区", "洪泽区", "涟水县", "盱眙县", "金湖县"],
    "盐城市": ["亭湖区", "盐都区", "大丰区", "响水县", "滨海县", "阜宁县", "射阳县",
            "建湖县", "东台市"],
    "扬州市": ["广陵区", "邗江区", "江都区", "宝应县", "仪征市", "高邮市"],
    "镇江市": ["京口区", "润州区", "丹徒区", "丹阳市", "扬中市", "句容市"],
    "泰州市": ["海陵区", "高港区", "姜堰区", "兴化市", "靖江市", "泰兴市"],
    "宿迁市": ["宿城区", "宿豫区", "沭阳县", "泗阳县", "泗洪县"],
    "杭州市": ["上城区", "拱墅区", "西湖区", "滨江区", "萧山区", "余杭区", "富阳区",
            "临安区", "临平区", "钱塘区", "桐庐县", "淳安县", "建德市"],
    "宁波市": ["海曙区", "江北区", "镇海区", "北仑区", "鄞州区", "奉化区", "象山县",
            "宁海县", "余姚市", "慈溪市"],
    "温州市": ["鹿城区", "龙湾区", "瓯海区", "洞头区", "永嘉县", "平阳县", "苍南县",
            "文成县", "泰顺县", "瑞安市", "乐清市"],
    "嘉兴市": ["南湖区", "秀洲区", "嘉善县", "海盐县", "海宁市", "平湖市", "桐乡市"],
    "湖州市": ["吴兴区", "南浔区", "德清县", "长兴县", "安吉县"],
    "绍兴市": ["越城区", "柯桥区", "上虞区", "新昌县", "诸暨市", "嵊州市"],
    "金华市": ["婺城区", "金东区", "武义县", "浦江县", "磐安县", "兰溪市", "义乌市",
            "东阳市", "永康市"],
    "衢州市": ["柯城区", "衢江区", "常山县", "开化县", "龙游县", "江山市"],
    "舟山市": ["定海区", "普陀区", "岱山县", "嵊泗县"],
    "台州市": ["椒江区", "黄岩区", "路桥区", "三门县", "天台县", "仙居县", "温岭市",
            "临海市", "玉环市"],
    "丽水市": ["莲都区", "青田县", "缙云县", "遂昌县", "松阳县", "云和县", "庆元县",
            "景宁畲族自治县", "龙泉市"],
}

AREA_CITY = {
    "310000": "上海市", "31": "上海市",
    "320100": "南京市", "320200": "无锡市", "320300": "徐州市", "320400": "常州市",
    "320500": "苏州市", "320600": "南通市", "320700": "连云港市", "320800": "淮安市",
    "320900": "盐城市", "321000": "扬州市", "321100": "镇江市", "321200": "泰州市",
    "321300": "宿迁市",
    "330100": "杭州市", "330200": "宁波市", "330300": "温州市", "330400": "嘉兴市",
    "330500": "湖州市", "330600": "绍兴市", "330700": "金华市", "330800": "衢州市",
    "330900": "舟山市", "331000": "台州市", "331100": "丽水市",
}


def _city_of(proxy: dict) -> str:
    """从 `proxyInfo` 解析代理城市（`浙江-杭州市|默认代理` → 杭州市）。"""
    name = str((proxy or {}).get("name") or "")
    name = name.split("|")[0]
    if "-" in name:
        name = name.split("-", 1)[1]
    name = name.strip()
    if name and not name.endswith("市"):
        name += "市"
    if name in CITY_DISTRICTS:
        return name
    return AREA_CITY.get(str((proxy or {}).get("areaCode") or ""), "")


def detect_exit_city(account_id: str) -> str:
    """探测账号的**真实出口城市**（配置的代理城市不一定真生效，实测有落到佳木斯的）。"""
    from collections import Counter

    from app.services.publish.yxer_cli import run_yxer

    for kw in ("万达广场", "人民广场"):
        try:
            raw = run_yxer(["query", "locations", account_id, "--query", kw,
                            "--json"], timeout=60)
        except Exception:  # noqa: BLE001
            continue
        items = ((raw.get("data") or {}).get("dataList") or [])
        cities = Counter(
            str(((it.get("raw") or {}).get("address_info") or {}).get("city") or "")
            for it in items)
        cities.pop("", None)
        if cities:
            return cities.most_common(1)[0][0]
    return ""


def build_pool_by_accounts(*, max_pages: int = DEFAULT_MAX_PAGES,
                           existing: set[str] | None = None,
                           force: bool = False) -> dict:
    """按**账号的真实出口城市**分组建池（配置的代理城市可能没生效）。

    先用"万达广场/人民广场"探测出口城市；**同一城市的每个号都采一遍再合并**
    （实测各号对同一区县的命中差异很大，只用一个号会漏区）；`existing` 里已全覆盖
    的城市直接跳过（除非 `force`）。返回 regions / failed / skipped / covered。
    """
    from app.services.douyin_poi_city import build_city_pool
    from app.services.publish.yxer_cli import run_yxer

    raw = run_yxer(["accounts", "list", "抖音", "--json"], timeout=60)
    by_city: dict[str, list[tuple[str, str]]] = {}
    for a in raw.get("data") or []:
        aid = str(a.get("id") or "")
        configured = _city_of(a.get("proxyInfo") or {})
        actual = detect_exit_city(aid) or configured
        by_city.setdefault(actual, []).append((aid, configured))

    out: dict[str, list] = {}
    failed: list[str] = []
    skipped: list[str] = []
    existing = existing or set()
    covered = 0
    for city, entries in by_city.items():
        districts = CITY_DISTRICTS.get(city) or []
        if not entries or not districts:
            names = "/".join(sorted({c for _, c in entries if c}))
            skipped.append(f"配置{names or '?'}/出口{city or '?'}（不在江浙沪）")
            continue
        if not force and all(d in existing for d in districts):
            covered += 1                 # 该城市已全覆盖，不用重采
            continue
        got: dict[str, list] = {}
        for aid, _configured in entries:
            for d, pois in build_city_pool(aid, city, max_pages).items():
                have = {str(p.get("id")) for p in got.get(d, [])}
                got.setdefault(d, []).extend(
                    p for p in pois if str(p.get("id")) not in have)
        if not got:
            skipped.append(f"出口{city}（取不到当地 POI）")
            continue
        out.update(got)
        for _aid, configured in entries:
            if configured and configured != city:
                log.warning("douyin_poi_exit_mismatch", extra={
                    "event": "douyin_poi_exit_mismatch",
                    "configured": configured, "actual": city})
        log.warning("douyin_poi_area_done", extra={
            "event": "douyin_poi_area_done", "city": city, "regions": len(got)})
    return {"regions": out, "failed": failed, "skipped": skipped,
            "covered": covered}


def refresh_pool(*, max_pages: int = DEFAULT_MAX_PAGES,
                 force: bool = False) -> dict:
    """按**各账号的真实出口城市**建池并落盘；保留旧池里其它城市的成功数据。

    `force=False`（默认）时跳过已全覆盖的城市，只补缺口，速度快得多。
    """
    from app.services import douyin_store
    from app.services.publish import yxer

    regions = city_regions()
    if not regions:
        return {"ok": False, "error": "业务城市为空，请先配置区县"}
    try:
        from app.services.publish.yxer_cli import sync_api_key
        sync_api_key(yxer.yxer_conf().get("token"))
    except Exception:  # noqa: BLE001 - 同步 Key 失败交给后续调用如实报错
        pass

    existing = douyin_store.load_poi_pool().get("regions") or {}
    res = build_pool_by_accounts(max_pages=max_pages, existing=set(existing),
                                 force=force)
    merged = {**existing, **(res.get("regions") or {})}
    pool = {"regions": merged,
            "failed": res.get("failed") or [],
            "skipped": res.get("skipped") or [],
            "meta": {"built_at": now_local().strftime("%Y-%m-%d %H:%M"),
                     "regions": len(merged),
                     "pois": sum(len(v) for v in merged.values()),
                     "total_regions": len(regions),
                     "all_regions": regions}}

    def _replace(d: dict) -> None:
        d.clear()
        d.update(pool)

    douyin_store.update_poi_pool(_replace)
    meta = pool["meta"]
    log.warning("douyin_poi_pool_built", extra={
        "event": "douyin_poi_pool_built", "regions": meta["regions"],
        "pois": meta["pois"], "skipped": len(pool["skipped"])})
    return {"ok": True, "regions": meta["regions"], "pois": meta["pois"],
            "built_at": meta["built_at"], "failed": len(pool["failed"]),
            "skipped": pool["skipped"], "covered": res.get("covered", 0)}
