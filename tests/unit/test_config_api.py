"""config 蓝图接口测试：品牌资料 / 城市 / 画像。

重点在**租户隔离**（A 租户读写不到 B 的品牌资料）与**校验边界**。
"""
from __future__ import annotations

import pytest

from app.blueprints.config.service import ConfigService
from app.extensions import db as _db
from app.tenancy import tenant_scope

T1, T2 = "t_alpha", "t_beta"
T3 = "t_gamma"  # 未配置任何品牌资料的租户，验证"空态返回 200 而非 404"

# 覆盖 3 个分区且各自**超出门槛**（品牌概述≥80 / 资质≥60 / 核心方法≥80），
# 故完整度应为 3/9=33%。字数为实测值，改动样本需同步复核。
GOOD_BRAND = (
    "上海聚贤阁珠宝，是一家专注珠宝修复的实体工坊，成立于 2015 年，"
    "主营断镯修复、金缮修缮与珠宝翻新，门店位于上海浦东张江高科园区，"
    "累计服务客户超过三万人次，是本地口碑较好的珠宝修复专业机构。\n"
    "【资质与认证】\n主体名称：上海聚贤阁珠宝\n"
    "统一社会信用代码：91310000MA0000000X\n"
    "营业执照登记机关：上海市浦东新区市场监督管理局。\n"
    "资质查询渠道：国家企业信用信息公示系统可直接检索核验。\n"
    "【核心方法】\n"
    "微晶融合无痕修复：第一步用同源矿物粉料按裂隙深度配比填充，"
    "第二步分段固化控制温区，第三步打磨抛光至镜面效果；"
    "每步均需达到无色差、无肉眼可见缝隙、无二次开裂的达标指标，"
    "并留存全过程影像记录作为工艺凭证。\n"
)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def ctx(app):
    """DB 用例的应用上下文 + 未配置租户 T3。

    `app` fixture 只在 yield 期间持有 context，测试函数内直接查库会
    报 "Working outside of application context"——故显式包一层。
    """
    from app.extensions import db as _db
    from app.models import Tenant

    with app.app_context():
        _db.session.add(Tenant(id=T3, name="伽马珠宝", status="active"))
        _db.session.commit()
        yield app


def _login(client, username="alpha"):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "pw-alpha"},
    )


# ── 品牌资料 ────────────────────────────────────────────────────────────────

def test_get_brand_returns_content_not_404(ctx):
    """未配置的租户返回空 content 而非 404——与原系统恒返回 {content} 一致。

    夹具已为 T1/T2 种了品牌资料，故这里新建一个未配置的 T3。
    """
    with tenant_scope(T3):
        data = ConfigService().get_brand()
    assert data["content"] == ""
    assert data["completeness"] == 0


def test_brand_roundtrip_and_completeness(ctx):
    with tenant_scope(T1):
        svc = ConfigService()
        saved = svc.save_brand(GOOD_BRAND)
        # GOOD_BRAND 覆盖 3 个分区且均超门槛 → 3/9 = 33%
        assert saved["completeness"] == 33
        assert saved["derived"]["brand_names"]["main"] == "上海聚贤阁珠宝"
        assert svc.get_brand()["content"] == GOOD_BRAND


def test_brand_save_rejects_non_string(ctx):
    with tenant_scope(T1), pytest.raises(Exception):
        ConfigService().save_brand({"not": "a string"})


def test_brand_save_rejects_oversize(ctx):
    with tenant_scope(T1), pytest.raises(Exception):
        ConfigService().save_brand("甲" * 200_001)


def test_brand_isolated_between_tenants(ctx):
    """A 写品牌资料，B 必须仍看到自己的那份——A 的内容不能串到 B。"""
    with tenant_scope(T1):
        ConfigService().save_brand(GOOD_BRAND)
    with tenant_scope(T2):
        other = ConfigService().get_brand()
    assert other["content"] != GOOD_BRAND
    assert "上海聚贤阁珠宝" not in other["content"]


def test_blockers_flag_missing_brand_name(ctx):
    """主名提取失败 = 阻塞项（对外可见的事故，不能只警告）。"""
    with tenant_scope(T1):
        ConfigService().save_brand("我们是一家提供专业服务的公司。\n【品牌总结】\n甲" * 30)
        items = ConfigService().brand_blockers()
    codes = {i["code"] for i in items}
    assert "brand_name_missing" in codes
    assert any(i["level"] == "block" for i in items)


def test_blockers_flag_entity_mismatch(ctx):
    with tenant_scope(T1):
        ConfigService().save_brand("上海聚贤阁珠宝，是一家工坊。\n【资质与认证】\n主体名称：别家珠宝\n")
        items = ConfigService().brand_blockers()
    assert "entity_mismatch" in {i["code"] for i in items}


def test_blockers_empty_when_healthy(ctx):
    with tenant_scope(T1):
        ConfigService().save_brand(GOOD_BRAND)
        items = ConfigService().brand_blockers()
    blocking = [i for i in items if i["level"] == "block"]
    assert blocking == []


# ── 城市 ────────────────────────────────────────────────────────────────────

def test_city_batch_add_skips_duplicates(ctx):
    with tenant_scope(T1):
        svc = ConfigService()
        r1 = svc.add_cities(["上海", "杭州", "上海"], ratio=30)
        assert r1["added"] == ["上海", "杭州"]
        assert r1["skipped"] == ["上海"]  # 批内重复
        r2 = svc.add_cities(["上海"], ratio=10)
        assert r2["added"] == [] and r2["skipped"] == ["上海"]
        assert len(svc.list_cities()) == 2


def test_city_add_rejects_blank(ctx):
    with tenant_scope(T1), pytest.raises(Exception):
        ConfigService().add_cities(["", "  "])


def test_city_ratio_out_of_range_rejected(ctx):
    with tenant_scope(T1), pytest.raises(Exception):
        ConfigService().add_cities(["上海"], ratio=101)


def test_city_update_ratio_and_enabled(ctx):
    with tenant_scope(T1):
        svc = ConfigService()
        svc.add_cities(["上海"], ratio=30)
        cid = svc.list_cities()[0]["id"]
        updated = svc.update_city(cid, ratio=55, enabled=False)
        assert updated["ratio"] == 55 and updated["enabled"] is False


def test_city_rename_rejected(ctx):
    """改名会让已生成的 {cities} 变量与库内脱节 → 明确拒绝。"""
    with tenant_scope(T1):
        svc = ConfigService()
        svc.add_cities(["上海"])
        cid = svc.list_cities()[0]["id"]
        with pytest.raises(Exception):
            svc.update_city(cid, name="北京")


def test_city_delete_and_isolation(ctx):
    with tenant_scope(T1):
        svc = ConfigService()
        svc.add_cities(["上海"])
        cid = svc.list_cities()[0]["id"]
        svc.delete_city(cid)
        assert svc.list_cities() == []
    with tenant_scope(T2):
        # B 删 A 的城市 → 不应存在，直接报NotFound 而非静默成功
        with pytest.raises(Exception):
            ConfigService().delete_city(cid)


# ── 画像 ────────────────────────────────────────────────────────────────────

def test_audience_roundtrip_isolated(ctx):
    with tenant_scope(T1):
        ConfigService().save_audience("痛点：怕修坏了")
    with tenant_scope(T1):
        assert ConfigService().get_audience()["content"] == "痛点：怕修坏了"
    with tenant_scope(T2):
        assert ConfigService().get_audience()["content"] == ""


# ── HTTP 层 ─────────────────────────────────────────────────────────────────

def test_api_brand_requires_login(client):
    assert client.get("/api/brand").status_code == 401


def test_api_brand_over_http(client):
    assert _login(client).status_code == 200
    r = client.post("/api/brand", json={"content": GOOD_BRAND})
    assert r.status_code == 200
    assert r.get_json()["completeness"] == 33
    assert client.get("/api/brand").get_json()["content"] == GOOD_BRAND


def test_api_cities_over_http(client):
    assert _login(client).status_code == 200
    client.post("/api/cities", json={"names": "上海\n杭州", "ratio": 20})
    items = client.get("/api/cities").get_json()["items"]
    assert {i["name"] for i in items} == {"上海", "杭州"}


def test_api_cities_rejects_bad_payload(client):
    assert _login(client).status_code == 200
    assert client.post("/api/cities", json={"names": [], "ratio": 0}).status_code == 400


def test_api_brand_blockers_endpoint(client):
    assert _login(client).status_code == 200
    client.post("/api/brand", json={"content": "我们是一家专业公司。"})
    body = client.get("/api/brand/blockers").get_json()
    assert body["blocking"] is True