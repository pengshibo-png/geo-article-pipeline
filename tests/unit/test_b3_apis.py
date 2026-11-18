"""B3 后端契约：cities 双形状、my-prompts、my-title-config、存量库补列。

页面契约依据 参考系统页面实测 与 参考系统接口实测
/`my_title_config.json` 实测形状（B3 还原的对接面）。
"""
from __future__ import annotations

import sqlite3

from tests.conftest import T1, T2, login

# ── cities 双形状 ───────────────────────────────────────────────────────────

def test_cities_get_dual_shape(client):
    login(client, "alpha", "pw-alpha")
    body = client.get("/api/cities").get_json()
    # 管理端形状（旧契约不动）
    assert isinstance(body["items"], list)
    # 客户页形状（原站 cities.html）
    assert body["cities"] == ["杭州", "上海"]      # 来自种子 TitleConfig.cities
    assert body["count"] == 2
    assert body["ratio"] == 60                     # 列默认值


def test_cities_post_sync_mode(client, app):
    """原站客户页 POST {cities, ratio} → {ok, count}：整份同步 + 回写 TitleConfig。"""
    login(client, "alpha", "pw-alpha")
    r = client.post("/api/cities", json={"cities": ["苏州", "上海", "上海", " "], "ratio": 80})
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"ok": True, "count": 2}        # 重名/空白剔除

    cfg = client.get("/api/cities").get_json()
    assert cfg["cities"] == ["苏州", "上海"]
    assert cfg["ratio"] == 80

    from app.extensions import db
    from app.models import TitleConfig
    with app.app_context():
        row = db.session.get(TitleConfig, T1)
        assert row.city_ratio == 80


def test_cities_sync_rebuilds_city_table(client, app):
    """整份同步是差量重建：不在名单里的城市删除，存量新混合各自归位。"""
    login(client, "alpha", "pw-alpha")
    client.post("/api/cities", json={"cities": ["苏州"], "ratio": 50})
    from app.extensions import db
    from app.models import City
    with app.app_context():
        names = {r.name for r in db.session.query(City).all()}
        # 种子没有 City 行；同步后 City 表应恰好反映名单（杭州/上海从未在 City 表）
        assert names == {"苏州"}

        # 旧加法模式不受影响（names 键仍走 add_cities）
        r = client.post("/api/cities", json={"names": "南京", "ratio": 10})
        assert r.status_code == 200
        names = {r.name for r in db.session.query(City).all()}
        assert names == {"苏州", "南京"}


def test_cities_sync_rejects_bad_ratio(client):
    login(client, "alpha", "pw-alpha")
    r = client.post("/api/cities", json={"cities": ["苏州"], "ratio": 120})
    assert r.status_code == 400


def test_cities_sync_tenant_isolated(client):
    login(client, "alpha", "pw-alpha")
    client.post("/api/cities", json={"cities": ["苏州"], "ratio": 50})
    login(client, "beta", "pw-beta")
    body = client.get("/api/cities").get_json()
    assert body["cities"] == []


# ── my-prompts ──────────────────────────────────────────────────────────────

def test_my_prompts_shape(client):
    login(client, "alpha", "pw-alpha")
    body = client.get("/api/my-prompts").get_json()
    prompts = body["prompts"]
    assert [p["step"] for p in prompts] == ["A", "B", "C", "T"]   # 原站顺序
    for p in prompts:
        assert set(p) == {"step", "name", "content", "desc", "is_custom", "length"}
        assert p["is_custom"] is False
        assert p["length"] == len(p["content"])
    names = {p["step"]: p["name"] for p in prompts}
    assert names == {"A": "生成器", "B": "审稿官", "C": "修改器", "T": "标题选题"}
    descs = {p["step"]: p["desc"] for p in prompts}
    assert descs["A"].startswith("AI写文章的核心指令")
    assert descs["T"].startswith("AI生成文章标题")


def test_my_prompt_save_and_reset(client):
    login(client, "alpha", "pw-alpha")
    r = client.post("/api/my-prompts/A", json={"content": "自定义写稿指令"})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "length": 7}

    prompts = {p["step"]: p for p in client.get("/api/my-prompts").get_json()["prompts"]}
    assert prompts["A"]["content"] == "自定义写稿指令"
    assert prompts["A"]["is_custom"] is True
    assert prompts["B"]["is_custom"] is False

    r = client.post("/api/my-prompts/A/reset")
    assert r.get_json() == {"ok": True}
    prompts = {p["step"]: p for p in client.get("/api/my-prompts").get_json()["prompts"]}
    assert prompts["A"]["is_custom"] is False


def test_my_prompt_unknown_step_404(client):
    login(client, "alpha", "pw-alpha")
    assert client.post("/api/my-prompts/Z", json={"content": "x"}).status_code == 404
    assert client.post("/api/my-prompts/Z/reset").status_code == 404


# ── my-title-config ─────────────────────────────────────────────────────────

def test_my_title_config_get_defaults(client):
    login(client, "alpha", "pw-alpha")
    body = client.get("/api/my-title-config").get_json()
    assert body["business"] == "珠宝修复"
    assert body["cities"] == ["杭州", "上海"]
    assert body["city_ratio"] == 60
    assert body["timeliness_ratio"] == 80
    # 无 GenreMix 行 → 出厂配比
    assert body["genre_mix"] == {"商业推荐": 80, "价格咨询": 10, "避坑对比": 5, "科普知识": 5}
    assert body["topics"] == []
    assert set(body) == {"business", "expert", "cities", "city_ratio", "topics",
                         "genre_mix", "timeliness_ratio"}


def test_my_title_config_save_normalizes_mix(client):
    login(client, "alpha", "pw-alpha")
    r = client.post("/api/my-title-config", json={
        "genre_mix": {"商业推荐": 50, "价格咨询": 30, "避坑对比": 20, "科普知识": 0},
        "timeliness_ratio": 80,
    })
    body = r.get_json()
    assert body["ok"] is True
    assert sum(body["genre_mix"].values()) == 100
    assert body["genre_mix"] == {"商业推荐": 50, "价格咨询": 30, "避坑对比": 20, "科普知识": 0}
    assert body["timeliness_ratio"] == 80

    # 回读一致
    got = client.get("/api/my-title-config").get_json()
    assert got["genre_mix"] == body["genre_mix"]


def test_my_title_config_save_clamps_and_scales(client):
    login(client, "alpha", "pw-alpha")
    # 非归一输入按比例缩放 + 取整误差归最大项；timeliness 越界收敛
    body = client.post("/api/my-title-config", json={
        "genre_mix": {"商业推荐": 200, "价格咨询": 100, "避坑对比": 0, "科普知识": 0},
        "timeliness_ratio": 999,
    }).get_json()
    assert sum(body["genre_mix"].values()) == 100
    assert body["genre_mix"] == {"商业推荐": 67, "价格咨询": 33, "避坑对比": 0, "科普知识": 0}
    assert body["timeliness_ratio"] == 100


def test_my_title_config_tenant_isolated(client):
    login(client, "alpha", "pw-alpha")
    client.post("/api/my-title-config", json={
        "genre_mix": {"商业推荐": 100, "价格咨询": 0, "避坑对比": 0, "科普知识": 0},
        "timeliness_ratio": 10,
    })
    login(client, "beta", "pw-beta")
    body = client.get("/api/my-title-config").get_json()
    assert body["genre_mix"]["商业推荐"] == 80
    assert body["timeliness_ratio"] == 25


# ── 画像编解码（行式文本 ↔ 结构对象）───────────────────────────────────────

def test_profile_codec_roundtrip():
    from app.blueprints.config.profile_codec import deserialize_profile, serialize_profile
    profile = {
        "singles": {"who": "30-55 岁业主", "age": "35-55 岁为主"},
        "lists": {"pain": ["反复漏水总修不好", "怕被乱报价"], "worry": []},
        "customs": [{"label": "咨询高频问题", "items": ["价格怎么算", "质保多久"]}],
    }
    text = serialize_profile(profile)
    assert "【人群描述】30-55 岁业主" in text
    assert "【核心痛点】\n- 反复漏水总修不好" in text
    assert "【自定义：咨询高频问题】" in text

    back = deserialize_profile(text)
    assert back["singles"]["who"] == "30-55 岁业主"
    assert back["singles"]["age"] == "35-55 岁为主"
    assert back["lists"]["pain"] == ["反复漏水总修不好", "怕被乱报价"]
    assert back["lists"].get("worry", []) == []      # 空列表序列化时跳过，不产生键
    assert back["customs"] == [{"label": "咨询高频问题", "items": ["价格怎么算", "质保多久"]}]

    # 二次往返稳定
    assert serialize_profile(back) == text


def test_profile_codec_keeps_free_text():
    """旧自由文本不静默丢失：整体归入人群描述。"""
    from app.blueprints.config.profile_codec import deserialize_profile
    pf = deserialize_profile("三十岁以上房东，老小区为多\n顶层渗水常见")
    assert "三十岁以上房东" in pf["singles"]["who"]
    assert pf["lists"] == {}


def test_audience_profile_roundtrip_over_http(client):
    login(client, "alpha", "pw-alpha")
    profile = {"singles": {"who": "30-55 岁业主"}, "lists": {"pain": ["怕漏水"]}, "customs": []}
    r = client.post("/api/audience", json={"profile": profile, "insight": ""})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    got = client.get("/api/audience").get_json()
    assert got["insight"] == ""
    assert got["profile"]["singles"]["who"] == "30-55 岁业主"
    assert got["profile"]["lists"]["pain"] == ["怕漏水"]
    assert "【人群描述】30-55 岁业主" in got["content"]   # content 仍是行式文本


def test_audience_analyze_enqueues(client):
    """AI 洞察已接真：入队成功 + 状态端点形状 {analyzing, log_tail, error, result}。"""
    login(client, "alpha", "pw-alpha")
    r = client.post("/api/audience/analyze", json={"profile": {"singles": {"who": "x"}, "lists": {}, "customs": []}})
    assert r.get_json().get("ok") is True
    s = client.get("/api/audience/analyze-status").get_json()
    assert set(s) == {"analyzing", "log_tail", "error", "result"}


# ── 素材库 stub ─────────────────────────────────────────────────────────────

def test_my_assets_stub(client):
    login(client, "alpha", "pw-alpha")
    assert client.get("/api/my-assets").get_json() == {"assets": [], "total": 0}
    up = client.post("/api/my-assets", data={}).get_json()
    assert up["ok"] is False and up["error"]


# ── 存量库补列（桌面升级路径）───────────────────────────────────────────────

def test_dbpatch_adds_city_ratio_to_legacy_db(tmp_path, monkeypatch):
    """模拟旧版桌面 app.db：title_config 无 city_ratio 列 → create_app 自动补列。"""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE title_config (tenant_id VARCHAR(64) PRIMARY KEY, business VARCHAR(255),"
        " cities TEXT, expert VARCHAR(128), directions TEXT, timeliness_ratio NUMERIC(5,2) NOT NULL,"
        " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO title_config (tenant_id, business, cities, expert, directions, timeliness_ratio)"
        " VALUES ('t_old', 'b', '上海', 'e', NULL, 30)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("GEO_ENV", "test")
    monkeypatch.setenv("GEO_DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("GEO_LLM_PROVIDER", "fake")
    from app import create_app
    from app.extensions import db
    from app.models import TitleConfig

    application = create_app("test")
    with application.app_context():
        row = db.session.get(TitleConfig, "t_old")
        assert row is not None
        assert row.city_ratio == 60                  # 旧行按列默认值补齐
        assert db.session.get(TitleConfig, "t_missing") is None

    # 幂等：二次启动不报错不重复
    application2 = create_app("test")
    with application2.app_context():
        assert db.session.get(TitleConfig, "t_old").city_ratio == 60
    with application.app_context():
        db.session.remove()
