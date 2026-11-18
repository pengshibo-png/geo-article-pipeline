"""抖音图文定位分配（services/douyin_location.py）的回归测试。

用户 2026-10-19 定的三层规则：
1. 账号间：同一批内每个账号的 POI 不同；
2. 同账号前后：同一账号本次地区 ≠ 上次地区；
3. 同地区：同一地区多次使用时地址不同。
外加：地址耗尽换区、全耗尽近期循环、池为空降级、建池过滤异地同名 + 翻页去重。
"""
from __future__ import annotations

from app.services import douyin_location as dl
from app.services import douyin_poi_build as db


def _poi(pid: str, name: str) -> dict:
    # 名称带「小区」以通过定位白名单（商场/中高端小区），保留对分配规则的验证
    return {"id": pid, "name": name + "小区", "raw": {"poi_id": pid}, "desc": ""}


def _pool(**regions) -> dict:
    return {"regions": {r: v for r, v in regions.items()}, "meta": {}}


# ── 分配：三层不重复 ────────────────────────────────────────────────────────

def test_allocate_unique_within_batch():
    """规则 1：同一批里 5 个账号拿到的 5 个 POI 必须互不相同。"""
    pool = _pool(黄浦区=[_poi("a1", "A1"), _poi("a2", "A2"), _poi("a3", "A3")],
                 徐汇区=[_poi("b1", "B1"), _poi("b2", "B2")])
    state: dict = {}
    pairs = [("1", f"yx-{i}") for i in range(1, 6)]
    out = dl.allocate(pairs, pool, state)
    ids = [v["data"]["yixiaoerId"] for v in out.values() if v]
    assert len(ids) == 5
    assert len(set(ids)) == 5, f"账号间定位不能重复：{ids}"


def test_allocate_same_account_changes_region():
    """规则 2：同一账号连续两次发布，地区必须不同。"""
    pool = _pool(黄浦区=[_poi("a1", "A1")], 徐汇区=[_poi("b1", "B1")])
    state: dict = {}
    dl.allocate([("1", "yx-1")], pool, state)
    first = state["accounts"]["yx-1"]["regions"][-1]
    dl.allocate([("2", "yx-1")], pool, state)
    second = state["accounts"]["yx-1"]["regions"][-1]
    assert first != second, f"同账号前后地区必须不同：{first} == {second}"


def test_allocate_same_region_different_address():
    """规则 3：同一地区被两个账号用到时，地址必须不同。"""
    pool = _pool(黄浦区=[_poi("a1", "A1"), _poi("a2", "A2")])
    state: dict = {}
    o1 = dl.allocate([("1", "yx-1")], pool, state)
    o2 = dl.allocate([("2", "yx-2")], pool, state)
    assert (o1[("1", "yx-1")]["data"]["yixiaoerId"]
            != o2[("2", "yx-2")]["data"]["yixiaoerId"])


def test_allocate_switches_region_when_exhausted():
    """某地区本轮地址用尽 → 换到还有余量的地区。"""
    pool = _pool(黄浦区=[_poi("a1", "A1")], 徐汇区=[_poi("b1", "B1")])
    out = dl.allocate([("1", "yx-1"), ("1", "yx-2")], pool, {})
    ids = {v["data"]["yixiaoerId"] for v in out.values()}
    assert ids == {"a1", "b1"}, f"用尽应换区：{ids}"


def test_allocate_cycles_across_batches_when_all_exhausted():
    """所有地区都用满一轮 → 下一批开始前重置本轮记录，允许复用（近期循环）。"""
    pool = _pool(黄浦区=[_poi("a1", "A1")], 徐汇区=[_poi("b1", "B1")])
    state: dict = {}
    first = dl.allocate([("1", "yx-1"), ("1", "yx-2")], pool, state)
    assert all(first.values())
    second = dl.allocate([("2", "yx-3")], pool, state)
    assert second[("2", "yx-3")] is not None, "全耗尽后下一批应重置循环并可复用"
    assert state["region_used"]["黄浦区"]["cycle"] == 2


def test_batch_beyond_pool_returns_none_not_duplicate():
    """同一批账号数 > 池内地址数：多出的号宁可不加定位，也不能重复（规则 1）。"""
    pool = _pool(黄浦区=[_poi("a1", "A1")], 徐汇区=[_poi("b1", "B1")])
    out = dl.allocate([("1", "yx-1"), ("1", "yx-2"), ("1", "yx-3")], pool, {})
    assert out[("1", "yx-3")] is None
    ids = [v["data"]["yixiaoerId"] for v in out.values() if v]
    assert len(set(ids)) == len(ids), "同批内不能重复"


def test_allocate_manual_override_wins():
    """发布弹窗手选：即使自动分配不会选它，也必须用手选的那个。"""
    pool = _pool(黄浦区=[_poi("a1", "A1"), _poi("a2", "A2")],
                 徐汇区=[_poi("b1", "B1")])
    state: dict = {}
    out = dl.allocate([("1", "yx-1")], pool, state,
                      overrides={("1", "yx-1"): "b1"})
    assert out[("1", "yx-1")]["data"]["yixiaoerId"] == "b1"
    assert "b1" in state["region_used"]["徐汇区"]["used"]


def test_allocate_preset_from_state_applied():
    """作品库预设存在 state['overrides'] 里，分配时必须生效。"""
    pool = _pool(黄浦区=[_poi("a1", "A1")], 徐汇区=[_poi("b1", "B1")])
    state: dict = {"overrides": {"1": {"yx-1": "b1"}}}
    out = dl.allocate([("1", "yx-1")], pool, state)
    assert out[("1", "yx-1")]["data"]["yixiaoerId"] == "b1"


def test_allocate_unknown_override_falls_back_to_auto():
    """手选的地址已不在池里（池刷新过）→ 回退自动，不能整条报废。"""
    pool = _pool(黄浦区=[_poi("a1", "A1")])
    out = dl.allocate([("1", "yx-1")], pool, {},
                      overrides={("1", "yx-1"): "gone"})
    assert out[("1", "yx-1")]["data"]["yixiaoerId"] == "a1"


def test_overrides_from_payload():
    assert dl.overrides_from_payload([
        {"pack_id": 1, "account_id": "yx-1", "poi_id": "p1"},
        {"pack_id": 2, "account_id": "yx-2", "poi_id": ""},
        {"pack_id": 3, "account_id": "yx-3"},
    ]) == {("1", "yx-1"): "p1"}


def test_allocate_empty_pool_returns_none():
    """池为空 → 全部 None（发布侧据此降级为"不加定位"）。"""
    out = dl.allocate([("1", "yx-1"), ("1", "yx-2")], {"regions": {}}, {})
    assert out == {("1", "yx-1"): None, ("1", "yx-2"): None}


def test_location_payload_shape():
    loc = dl._location_of({"id": "x", "name": "N", "raw": {"k": 1}})
    assert loc == {"data": {"yixiaoerId": "x", "yixiaoerName": "N",
                            "raw": {"k": 1}}, "isScp": False}


# ── 建池：过滤异地同名 + 翻页 + 去重 ────────────────────────────────────────

def test_build_pool_filters_paginates_and_dedupes(monkeypatch):
    pages = {
        None: {"data": {"dataList": [
            {"yixiaoerId": "a1", "yixiaoerName": "黄浦某小区",
             "raw": {"address_info": {"city": "上海市", "district": "黄浦区"}}},
            {"yixiaoerId": "bad", "yixiaoerName": "外地同名公司",
             "raw": {"address_info": {"city": "上海市", "district": "闵行区"}}},
        ], "nextPage": 2}},
        2: {"data": {"dataList": [
            {"yixiaoerId": "a2", "yixiaoerName": "黄浦另小区",
             "raw": {"address_info": {"city": "上海市", "district": "黄浦区"}}},
            {"yixiaoerId": "a1", "yixiaoerName": "重复小区",
             "raw": {"address_info": {"city": "上海市", "district": "黄浦区"}}},
        ], "nextPage": None}},
    }
    calls: list[list[str]] = []

    def fake(cmd, **_kw):
        calls.append(list(cmd))
        page = int(cmd[cmd.index("--next-page") + 1]) if "--next-page" in cmd else None
        return pages[page]

    monkeypatch.setattr(db, "run_yxer", fake)
    pool = db.build_pool(["黄浦区"], yxer_account_id="acct")
    assert [p["id"] for p in pool["regions"]["黄浦区"]] == ["a1", "a2"], \
        "应过滤异地同名、按 id 去重、并翻到第 2 页"
    assert len(calls) == 2


def test_build_pool_drops_regions_without_local_poi(monkeypatch):
    monkeypatch.setattr(db, "run_yxer",
                        lambda cmd, **_: {"data": {"dataList": [
                            {"yixiaoerId": "x", "yixiaoerName": "外地公司",
                             "raw": {"address_info": {"city": "上海市",
                                                      "district": "闵行区"}}},
                        ], "nextPage": None}})
    pool = db.build_pool(["嵊州市"], yxer_account_id="acct")
    assert "嵊州市" not in pool["regions"], "没有本地地址的地区应排除出池"
