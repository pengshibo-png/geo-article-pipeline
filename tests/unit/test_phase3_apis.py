"""Phase III 契约测试：register API + 发布执行器（GEO/报告后续补在本文件）。

register 形状依据 `app/static/register.js` submit() 实测（页面读 d.ok /
d.account / d.tenant{id,biz,cities,topic,accounts}，失败读 d.message||d.error）。
发布形状依据 参考系统页面实测 实测。
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.extensions import db
from app.models import Account, Article, Report
from app.tenancy import TenantScopedRepository, tenant_scope
from app.timeutil import now_local
from tests.conftest import T1, T2, login


@pytest.fixture(autouse=True)
def _reset_bridges():
    from app.blueprints.generation import douyin_jobs
    from app.services import geo_monitor
    from app.services.publish import executor
    from app.services.writer import managed
    managed.reset_states()
    executor.reset_task_flags()
    geo_monitor.reset_states()
    douyin_jobs.reset_states()
    yield
    managed.reset_states()
    executor.reset_task_flags()
    geo_monitor.reset_states()
    douyin_jobs.reset_states()


def _add_account(app, acc_id, *, platform="meipian", limit=5, tenant=T1, name=None):
    with app.app_context():
        db.session.add(Account(id=acc_id, tenant_id=tenant, platform=platform,
                               name=name or f"账号{acc_id}", daily_limit=limit,
                               used_today=0, quota_date=date.fromisoformat(
                                   str(date.today()))))
        db.session.commit()


def _make_final(app, dir_name, *, tenant=T1, title=None):
    from app.platform import storage

    with app.app_context():
        with tenant_scope(tenant):
            storage.write_article_file(dir_name, storage.DRAFT, f"<p>{dir_name} 初稿</p>")
            storage.write_article_file(dir_name, storage.FINAL, f"<p>{dir_name} 终稿</p>")
            row = TenantScopedRepository(Article).first(dir=dir_name)
            if row is None:
                TenantScopedRepository(Article).add(
                    Article(dir=dir_name, title=title or dir_name, status="final"))
            db.session.commit()

FORM = {
    "id": "gamma", "name": "伽马工作室", "brand": "伽马维修", "pass": "pw-gamma",
    "phones": ["13800000001", "400-000-000"], "biz": ["水管维修", "防水补漏"],
    "cities": "广州,深圳\n佛山", "master": {"name": "陈师傅", "years": "18", "certs": "电工证"},
    "brand_doc": "", "dy_topic": "维修日常", "dy_images": 7, "dy_interval": 60,
    "dy_accounts": ["a1", "a2"], "dy_auto": False, "dy_enabled": True,
}


def test_register_opens_full_workspace(client):
    r = client.post("/api/tenants/register", json=FORM)
    body = r.get_json()
    assert r.status_code == 200
    assert body["ok"] is True and body["account"] == "gamma"
    assert body["tenant"] == {"id": "gamma", "biz": ["水管维修", "防水补漏"],
                              "cities": 3, "topic": "维修日常", "accounts": 2}

    # 「标识 + 密码」即登录（role=client，数据落到新租户）
    assert login(client, "gamma", "pw-gamma").get_json()["tenant"] == "gamma"
    tc = client.get("/api/my-title-config").get_json()
    assert tc["business"] == "水管维修、防水补漏"
    assert tc["cities"] == ["广州", "深圳", "佛山"]

    # 品牌骨架首句可解析主名 → 不阻塞生成
    brand = client.get("/api/brand").get_json()
    assert "伽马维修，是提供" in brand["content"]
    assert "【联系方式】" in brand["content"] and "陈师傅" in brand["content"]
    assert brand["derived"]["brand_names"]["confirmed"] is True


def test_register_brand_doc_wins_over_skeleton(client):
    form = dict(FORM, id="delta", brand_doc="_delta 咨询，是上海的一家管理咨询公司。\n【品牌概述】\n只做增长。\n")
    assert client.post("/api/tenants/register", json=form).get_json()["ok"] is True
    login(client, "delta", "pw-gamma")
    assert "管理咨询" in client.get("/api/brand").get_json()["content"]


def test_register_validations(client):
    for bad, frag in (
        ({"id": "", "name": "x", "pass": "123456"}, "租户标识"),
        ({"id": "BAD ID", "name": "x", "pass": "123456"}, "租户标识"),
        ({"id": "ok1", "name": "", "pass": "123456"}, "企业名称"),
        ({"id": "ok1", "name": "n", "pass": "12345"}, "密码"),
    ):
        body = client.post("/api/tenants/register", json=bad).get_json()
        assert body["ok"] is False and frag in body["message"], bad

    # 重名标识
    assert client.post("/api/tenants/register", json=FORM).get_json()["ok"] is True
    body = client.post("/api/tenants/register", json=FORM).get_json()
    assert body["ok"] is False and "已存在" in body["message"]


def test_register_is_public(client):
    """匿名可提交（PUBLIC_ENDPOINTS 钉死 auth.register_api）。"""
    body = client.post("/api/tenants/register", json=FORM).get_json()
    assert body["ok"] is True


def test_admin_provision_tenant_full_loop(client, app):
    """运营端开通新客户闭环：register → /api/me 立即可见 → 带 reason 切入视角。"""
    login(client, "root", "pw-root")
    form = {"id": "omega", "name": "欧米伽维修", "pass": "pw-omega",
            "biz": ["水管维修"], "cities": "杭州,上海", "phones": ["13800000000"]}
    r = client.post("/api/tenants/register", json=form)
    assert r.get_json()["ok"] is True and r.get_json()["account"] == "omega"

    me = client.get("/api/me").get_json()
    assert "omega" in [t["id"] for t in me["tenants"]]     # 页面 reload 的数据源

    v = client.post("/api/admin/view-tenant",
                    json={"tenant_id": "omega", "reason": "新客户交付初始化"})
    assert v.get_json()["ok"] is True
    assert client.get("/api/me").get_json()["tenant"] == "omega"
    # 新租户的品牌骨架首句可解析主名（R2 不阻塞生成）
    assert "欧米伽维修" in client.get("/api/brand").get_json()["content"]
    assert "水管维修" in client.get("/api/my-title-config").get_json()["business"]


# ── 发布执行器（fake 驱动，同步等待结果）───────────────────────────────────

def test_publish_one_success_updates_everything(client, app):
    login(client, "alpha", "pw-alpha")
    _add_account(app, "mp-1")
    _make_final(app, "测发布文章甲")

    pick = client.get("/api/publish-pick").get_json()
    assert pick["ok"] is True and pick["account_id"] == "mp-1"

    r = client.post("/api/publish-one", json={"dir": "测发布文章甲", "account_id": "mp-1"}).get_json()
    assert r["ok"] is True and r["url"].startswith("https://fake.example.com/meipian/")

    # 档案页（archives.html）：已发布 + 渠道徽章
    arts = client.get("/api/articles").get_json()["articles"]
    a = arts[0]
    assert a["published"] is True and a["status"] == "published"
    assert a["publish_entries"] == [{"platform": "meipian", "success": True, "url": r["url"]}]

    # 发布日志（layout/root/publog 消费）
    logs = client.get("/api/publish-log").get_json()
    assert logs["total"] == 1
    L = logs["logs"][0]
    assert L["success"] is True and L["title"] == "测发布文章甲" and L["platform"] == "meipian"

    # 任务中心（tasks.html 读 /api/tasks）
    tk = client.get("/api/tasks").get_json()
    assert tk["running"] is False and tk["tasks"][0]["status"] == "success"
    assert tk["tasks"][0]["url"] == r["url"]
    assert any("✅" in line for line in tk["log_tail"])


def test_publish_one_idempotent_same_day(client, app):
    """重复发布 = 事故 → 同文章同账号同自然日幂等复用，日志只 1 条。"""
    login(client, "alpha", "pw-alpha")
    _add_account(app, "mp-1", limit=10)
    _make_final(app, "测发布文章乙")
    r1 = client.post("/api/publish-one", json={"dir": "测发布文章乙", "account_id": "mp-1"}).get_json()
    r2 = client.post("/api/publish-one", json={"dir": "测发布文章乙", "account_id": "mp-1"}).get_json()
    assert r1["ok"] is True and r2["ok"] is True
    assert client.get("/api/publish-log").get_json()["total"] == 1


def test_publish_one_captcha_failure_freezes_flow(client, app):
    login(client, "alpha", "pw-alpha")
    _add_account(app, "fail-1", name="触发验证码号")
    _make_final(app, "测发布文章丙")
    r = client.post("/api/publish-one", json={"dir": "测发布文章丙", "account_id": "fail-1"}).get_json()
    assert r["ok"] is False and "验证码" in r["error"]
    assert client.get("/api/articles").get_json()["articles"][0]["published"] is False
    tk = client.get("/api/tasks").get_json()
    assert tk["tasks"][0]["status"] == "failed"


def test_publish_one_rejects_virtual_channel(client, app):
    login(client, "alpha", "pw-alpha")
    _add_account(app, "mp-1")
    _add_account(app, "mp-2", platform="meipian")
    _make_final(app, "测发布文章丁")
    r = client.post("/api/publish-one", json={"dir": "测发布文章丁", "account_id": "mp-all"}).get_json()
    assert r["ok"] is False and ("渠道" in r["error"] or "账号" in r["error"])
    # 但排队发布里勾选虚拟聚合项 = 展开为该平台全部真实账号
    q = client.post("/api/client/queue-publish",
                    json={"dirs": ["测发布文章丁"], "accounts": ["mp-all"]}).get_json()
    assert q["ok"] is True and q["queued"] == 2      # mp-1 + mp-2 各发一次


def test_publish_quota_exhausted(client, app):
    login(client, "alpha", "pw-alpha")
    _add_account(app, "mp-1", limit=1)
    _make_final(app, "额度文章一")
    _make_final(app, "额度文章二")
    r1 = client.post("/api/publish-one", json={"dir": "额度文章一", "account_id": "mp-1"}).get_json()
    assert r1["ok"] is True
    r2 = client.post("/api/publish-one", json={"dir": "额度文章二", "account_id": "mp-1"}).get_json()
    assert r2["ok"] is False and "额度" in r2["error"]
    assert client.get("/api/publish-pick").get_json()["ok"] is False   # 全渠道今日无余量


def test_queue_publish_smart_rotation(client, app):
    login(client, "alpha", "pw-alpha")
    _add_account(app, "mp-1", platform="meipian")
    _add_account(app, "sh-1", platform="souhu")
    for i in range(3):
        _make_final(app, f"轮转文章{i}")
    q = client.post("/api/client/queue-publish", json={"dirs": [f"轮转文章{i}" for i in range(3)]}).get_json()
    assert q["ok"] is True and q["queued"] == 3
    import time as _t
    deadline = _t.time() + 20
    while _t.time() < deadline:
        tk = client.get("/api/tasks").get_json()
        if not tk["running"]:
            break
        _t.sleep(0.2)
    plats = sorted(t["platform"] for t in tk["tasks"])
    assert plats == ["meipian", "meipian", "souhu"] or plats == ["meipian", "souhu", "souhu"]
    assert all(t["status"] == "success" for t in tk["tasks"])


def test_tasks_stop_and_resume(client, app):
    login(client, "alpha", "pw-alpha")
    assert client.post("/api/tasks/stop").get_json() == {"ok": True}
    assert client.post("/api/tasks/run").get_json()["ok"] is True


def test_preflight_shape(client, app):
    login(client, "alpha", "pw-alpha")
    _add_account(app, "mp-1")
    d = client.get("/api/client/preflight").get_json()
    # 未配 Cookie / 未绑蚁小二的账号必须报不健康（以前恒 ok=True 是假体检）
    assert d["checking"] is False
    assert d["bad"] == 1
    assert d["channels"] == [{"id": "mp-1", "name": "账号mp-1", "platform": "meipian",
                              "ok": False, "status": "active",
                              "reason": "未配置 Cookie / 蚁小二账号，或账号非活跃"}]


def test_autosched_publish_plans(client, app):
    """tasks.html 发布计划：accounts/articles_ready 真实返回，plans 整份 JSON 落库读回。"""
    with app.app_context():
        db.session.add(Account(id="mp-9", tenant_id=T1, platform="meipian", name="美篇号",
                               daily_limit=5, used_today=0, quota_date=date.today()))
        db.session.commit()
    login(client, "alpha", "pw-alpha")

    d = client.get("/api/autosched").get_json()
    assert d["publish_plans"] == [] and d["articles_ready"] == 0
    assert d["accounts"] == [{"id": "mp-9", "name": "美篇号", "platform": "meipian"}]

    plans = [{"id": 123, "channel": "meipian", "account_id": "mp-9", "account_name": "美篇号",
              "count": 10, "time": "08:30", "mode": "daily", "every_min": 30, "enabled": True},
             {"garbage": "x"}]
    assert client.post("/api/autosched", json={"publish_plans": plans}).get_json() == {"ok": True}
    back = client.get("/api/autosched").get_json()["publish_plans"]
    assert back[0] == {"id": 123, "channel": "meipian", "account_id": "mp-9",
                       "account_name": "美篇号", "count": 10, "time": "08:30",
                       "every_min": 30, "enabled": True}
    assert back[1]["account_id"] == "" and back[1]["enabled"] is False   # 残缺行按默认补齐


def test_register_publish_channel_excludes_register_type(client, app):
    """daily_limit=0 登记型账号：不进渠道、不可发布（A23 约定）。"""
    login(client, "alpha", "pw-alpha")
    _add_account(app, "reg-1", limit=0)
    assert client.get("/api/client/channels").get_json() == {"channels": []}
    assert client.get("/api/publish-pick").get_json()["ok"] is False


def test_article_channels_exclude_non_article_platforms(client, app):
    """抖音图文等非文章平台不进文章发布渠道。

    账号表里抖音图文号与文章号混在一起，但它们走 imageText 产线，文章驱动
    对它们必然报"平台不在文章发布支持范围"——展示了用户一点就报错。
    聚合项的口径也必须同步收窄，否则"全部自有账号"仍会把它们排进队列。
    """
    login(client, "alpha", "pw-alpha")
    _add_account(app, "dy-1", platform="douyin_imgtext", limit=10)
    _add_account(app, "sh-1", platform="souhu", limit=10)
    chs = client.get("/api/client/channels").get_json()["channels"]
    ids = {c["id"] for c in chs}
    assert "sh-1" in ids, "文章型平台必须在渠道里"
    assert "dy-1" not in ids, "抖音图文不该出现在文章发布渠道"
    agg = [c for c in chs if c["id"] == "tenant-all"]
    assert agg and "(1个)" in agg[0]["name"], "聚合项只应统计文章型账号"


def test_publish_one_rejects_non_article_platform(client, app):
    """直接调接口也不能绕过滤：非文章型平台如实拒绝，而不是等驱动报错。"""
    login(client, "alpha", "pw-alpha")
    _add_account(app, "dy-2", platform="douyin_imgtext", limit=10)
    _make_final(app, "dir-dy")
    body = client.post("/api/publish-one",
                       json={"dir": "dir-dy", "account_id": "dy-2"}).get_json()
    assert body["ok"] is False and "渠道不可用" in body["error"]


def test_publish_pick_ignores_non_article_platforms(client, app):
    """智能分配只从文章型账号里挑；全是抖音号时如实报"没有可用渠道"。"""
    login(client, "alpha", "pw-alpha")
    _add_account(app, "dy-3", platform="douyin_imgtext", limit=10)
    assert client.get("/api/publish-pick").get_json()["ok"] is False


# ── GEO 监测执行器 ──────────────────────────────────────────────────────────

def _poll_geo(client, timeout=40):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get("/api/geo/status").get_json()
        if not s["running"]:
            return s
        time.sleep(0.2)
    return s


def test_geo_run_end_to_end(client, app):
    """虚拟收录查询端到端：采样 → 落库(simulate) → 按日聚合。"""
    login(client, "alpha", "pw-alpha")
    client.post("/api/titles", json={"mode": "append", "titles": "阿尔法珠宝的断镯修复靠谱吗"})
    r = client.post("/api/geo/run", json={"engines": ["doubao"], "rounds": 1}).get_json()
    assert r["ok"] is True and r["running"] is True

    s = _poll_geo(client)
    assert s["error"] is None
    assert s["progress"]["done"] == 1 and s["progress"]["total"] == 1   # 1 引擎 × 1 轮
    assert any("模拟" in line for line in s["log_tail"])

    d = client.get("/api/geo/dashboard").get_json()
    assert d["date"] != ""
    assert d["overall"]["valid"] == 3                 # 3 问句 × 1 引擎
    assert d["per_engine"]["doubao"]["valid"] == 3
    assert d["per_engine"]["doubao"]["cite_rate"] == 0   # 模拟通道不产生真实链接
    assert len(d["trend"]) == 1 and d["trend"][0]["date"] == d["date"]

    # 全部落 simulate 通道，不与真实通道混算
    from app.models import GeoResult

    with app.app_context():
        with tenant_scope(T1):
            rows = db.session.query(GeoResult).filter_by(channel="simulate").all()
            assert len(rows) == 3


def test_geo_run_conflict_and_empty_library(client, app):
    login(client, "alpha", "pw-alpha")
    from app.services import geo_monitor
    geo_monitor.begin(T1, 0)
    assert client.post("/api/geo/run", json={}).get_json()["ok"] is False

    login(client, "beta", "pw-beta")
    client.post("/api/titles", json={"mode": "single_delete", "title": "上海软件开发公司怎么选"})
    client.post("/api/geo/run", json={})
    s = _poll_geo(client)
    assert s["running"] is False
    assert any("选题库为空" in line for line in s["log_tail"])


def test_geo_metrics_recompute_exact(client, app):
    """口径正确性（AI-ARCH-002）：4 样本 = 提及3 / 首位1 / 前三2 / 被引1。"""
    from app.models import GeoResult
    from app.services.geo_monitor import recompute_daily

    login(client, "alpha", "pw-alpha")
    with app.app_context():
        with tenant_scope(T1):
            base = now_local()
            for i, (m, rank, cite) in enumerate([(True, 1, False), (True, 2, False),
                                                 (False, None, False), (True, None, True)]):
                db.session.add(GeoResult(
                    query=f"q{i}", engine="doubao", channel="api", tenant_id=T1,
                    mentioned=m, rank=rank, cited_lv=cite, sampled_at=base,
                    cited_url="https://fake.example.com/meipian/断镯修复攻略" if cite else None))
            db.session.commit()
            assert recompute_daily(base.date()) == 1
            db.session.commit()

    d = client.get("/api/geo/dashboard").get_json()
    assert d["overall"] == {"valid": 4, "mention_rate": 75.0, "top_rate": 25.0,
                            "top3_rate": 50.0, "cite_rate": 25.0}
    assert d["cited_ours"][0]["engine"] == "doubao"
    assert d["cited_ours"][0]["platform"] == "meipian"
    assert d["cited_ours"][0]["url"].endswith("断镯修复攻略")
    # AI 引用份额（前端引用份额卡依赖 d.sov；修复前缺失→卡片恒为 0）
    assert d["sov"] == {"total": 3, "ours": 1, "rate": 25.0}


def test_cite_detection_uses_tenant_published_urls(app):
    """被引判定以租户真实发布 URL 为准（修复前只认 fake/meipian，真渠道全漏判）。"""
    from app.models import PublishLog
    from app.services.geo_monitor import _detect_cite, _known_url_keys, _url_key

    with app.app_context():
        with tenant_scope(T1):
            db.session.add(PublishLog(task_id=1, article_dir="甲", title="甲",
                                      account_id="ac-1", account_name="搜狐号A",
                                      platform="souhu", mode="real", success=True,
                                      url="https://www.sohu.com/a/987654321_123456",
                                      happened_at=now_local()))
            # 失败发布不算（没真发出去，AI 也不可能引到）
            db.session.add(PublishLog(task_id=2, article_dir="乙", title="乙",
                                      account_id="ac-1", account_name="搜狐号A",
                                      platform="souhu", mode="real", success=False,
                                      url="https://www.sohu.com/a/111111111_000000",
                                      happened_at=now_local()))
            db.session.commit()
            known = _known_url_keys()
            assert _url_key("https://www.sohu.com/a/987654321_123456") in known
            assert _url_key("https://www.sohu.com/a/111111111_000000") not in known

            # 真渠道链接能被引（含大小写/尾部标点差异，AI 回答里常见）
            assert _detect_cite(
                "推荐阅读：https://www.sohu.com/a/987654321_123456。", known)
            assert _detect_cite(
                "见 HTTPS://WWW.SOHU.COM/A/987654321_123456", known)
            # 别人的文章不算被引
            assert _detect_cite("https://www.sohu.com/a/555555555_999999", known) is None
            # 演示域仍保留（离线演示/现有口径不破坏）
            assert _detect_cite("https://fake.example.com/meipian/x", known)
            # 短 URL 不再误判：修复前双向子串包含会让 https://a.com 命中任意含它的长 URL
            known.add(_url_key("https://a.com"))
            assert _detect_cite("https://b.com/a.com/别人的文章", known) is None, \
                "别人的文章只是路径里含 a.com，不能算被引"


# ── 诊断报告中心 ────────────────────────────────────────────────────────────

def _poll_report(client, rid, timeout=40):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get("/api/report/status", query_string={"id": rid}).get_json()
        if s["status"] in ("done", "failed"):
            return s
        time.sleep(0.2)
    return s


def test_report_gate_no_data(client, app):
    """无任何数据 → 生成被拒（400 中文文案，页面原样展示）。"""
    login(client, "beta", "pw-beta")
    r = client.post("/api/report/generate", json={})
    assert r.status_code == 400 and "还没有" in r.get_json()["error"]


def test_report_full_lifecycle(client, app):
    login(client, "alpha", "pw-alpha")
    _make_final(app, "报告样本文章", title="报告样本文章")   # 有内容 → 过门禁

    r = client.post("/api/report/generate", json={})
    assert r.status_code == 200
    rid = r.get_json()["report_id"]
    assert rid.startswith("r_") and T1 in rid

    s = _poll_report(client, rid)
    assert s["status"] == "done", s

    # list（页面读 total/pages/rows/quota）
    lst = client.get("/api/report/list", query_string={"page": 1, "size": 20}).get_json()
    assert lst["total"] == 1 and lst["pages"] == 1
    row = lst["rows"][0]
    assert row["id"] == rid and row["status"] == "done" and row["summary_line"]
    assert row["window_from"] and row["window_to"]
    assert lst["quota"] == {"limit": 25, "used": 1, "total_cost": 0.02}

    # html 正文可读
    h = client.get("/api/report/html", query_string={"id": rid})
    assert h.status_code == 200 and "GEO 诊断报告" in h.get_data(as_text=True)

    # 分享签发 → 列表带 token → 停止 → token 清空
    sh = client.post("/api/report/share", json={"id": rid, "days": 30}).get_json()
    assert sh["ok"] is True and sh["token"] and sh["expires_at"]
    row = client.get("/api/report/list").get_json()["rows"][0]
    assert row["share_token"] == sh["token"]
    # 前端分享下拉依赖嵌套 share 对象（active → 有 token/expires_at）
    assert row["share"] == {"active": True, "token": sh["token"],
                            "expires_at": sh["expires_at"]}
    assert client.post("/api/report/share/revoke", json={"token": sh["token"]}).get_json() == {"ok": True}
    assert client.get("/api/report/list").get_json()["rows"][0]["share_token"] == ""
    # 撤回后 share 对象为 None（下拉不渲染"分享中"）
    assert client.get("/api/report/list").get_json()["rows"][0]["share"] is None


def _shared_report(client, app, *, days=30, active=True, expire_days=30):
    """造一份 done 报告并签发分享，返回 (token, rid)。"""
    login(client, "alpha", "pw-alpha")
    _make_final(app, "分享样本文章")
    rid = client.post("/api/report/generate", json={}).get_json()["report_id"]
    _poll_report(client, rid)
    sh = client.post("/api/report/share", json={"id": rid, "days": days}).get_json()
    if not active or expire_days != 30:
        with app.app_context():
            with tenant_scope(T1):
                rep = db.session.get(Report, rid)
                if not active:
                    rep.share_active = False
                if expire_days != 30:
                    from datetime import timedelta
                    rep.share_expires = now_local() - timedelta(days=1)
                db.session.commit()
    return sh["token"], rid


def test_report_share_page_is_public(client, app):
    """/r/<token> 匿名可看（原站语义：token 即授权，无需登录）。"""
    token, rid = _shared_report(client, app)
    r = client.get(f"/r/{token}")
    assert r.status_code == 200
    assert "GEO 诊断报告" in r.get_data(as_text=True)


def test_report_share_page_rejects_unknown_token(client):
    assert client.get("/r/not-a-real-token").status_code == 404


def test_report_share_page_rejects_revoked(client, app):
    token, _ = _shared_report(client, app, active=False)
    assert client.get(f"/r/{token}").status_code == 404


def test_report_share_page_rejects_expired(client, app):
    token, _ = _shared_report(client, app, expire_days=-1)
    assert client.get(f"/r/{token}").status_code == 404


def test_report_share_page_serves_other_tenants_report(client, app):
    """A 租户签发的分享，匿名（无租户上下文）也能读到 —— 文件按报告自己的租户解析。"""
    token, _ = _shared_report(client, app)
    client.get("/api/auth/logout")                 # 登出 → 无会话无租户
    r = client.get(f"/r/{token}")
    assert r.status_code == 200 and "GEO 诊断报告" in r.get_data(as_text=True)


def test_report_regen_and_retry(client, app):
    login(client, "alpha", "pw-alpha")
    _make_final(app, "报告重试样本")
    rid = client.post("/api/report/generate", json={}).get_json()["report_id"]
    _poll_report(client, rid)
    # done 报告 retry → 拒绝；只有 failed 可重试
    rr = client.post("/api/report/retry", json={"id": rid, "step": ""})
    assert rr.status_code == 400

    with app.app_context():
        with tenant_scope(T1):
            from app.models import Report
            rep = db.session.get(Report, rid)
            rep.status = "failed"
            rep.note = "测试失败"
            db.session.commit()
    r2 = client.post("/api/report/retry", json={"id": rid})
    assert r2.get_json()["report_id"] == rid
    assert _poll_report(client, rid)["status"] == "done"


def test_report_quota_exhausted(client, app):
    from app.models import ReportQuota
    login(client, "alpha", "pw-alpha")
    _make_final(app, "额度样本")
    with app.app_context():
        with tenant_scope(T1):
            q = TenantScopedRepository(ReportQuota).add(
                ReportQuota(tenant_id=T1, quota_limit=25, used=25, total_cost=0.5))
            db.session.commit()
    r = client.post("/api/report/generate", json={})
    assert r.status_code == 400 and "额度" in r.get_json()["error"]


# ── 素材库（真实上传/服务/删除 → 写稿取用）─────────────────────────────────

def test_assets_upload_list_serve_delete(client, app):
    import io

    login(client, "alpha", "pw-alpha")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = client.post("/api/my-assets", data={"files": (io.BytesIO(png), "主图.png")},
                    content_type="multipart/form-data")
    body = r.get_json()
    assert body["ok"] is True and body["added"] == 1 and body["total"] == 1

    a = client.get("/api/my-assets").get_json()["assets"][0]
    assert a["name"] == "主图.png" and a["url"].startswith("/media/assets/")
    served = client.get(a["url"])
    assert served.status_code == 200 and served.data == png and served.mimetype == "image/png"

    bad = client.post("/api/my-assets", data={"files": (io.BytesIO(b"x" * 10), "a.exe")},
                      content_type="multipart/form-data").get_json()
    # 全部被拒必须 ok:False——否则前端弹"上传成功"却新增 0 张。
    assert bad["ok"] is False and bad["added"] == 0 and bad["errors"]
    assert "没有素材入库" in bad["error"]
    big = client.post("/api/my-assets",
                      data={"files": (io.BytesIO(b"1" * (5 * 1024 * 1024 + 1)), "big.png")},
                      content_type="multipart/form-data").get_json()
    assert big["ok"] is False and big["added"] == 0 and any("5MB" in e for e in big["errors"])

    assert client.post("/api/my-assets/delete", json={"id": a["id"]}).get_json() == {"ok": True}
    assert client.get("/api/my-assets").get_json()["total"] == 0
    assert client.get(a["url"]).status_code == 404    # 文件随行删除


# ── 蚁小二（yxer）多平台一键发布 ───────────────────────────────────────────

def test_yxer_not_configured_empty_state(client):
    """未接入：云列表返回 configured=False + 空列表（页面走空态引导）。"""
    login(client, "alpha", "pw-alpha")
    body = client.get("/api/yxer-accounts").get_json()
    assert body["configured"] is False and body["accounts"] == []


def test_yxer_mock_sync_and_multi_publish(client, app):
    """演示模式全链路：云列表 → 勾选同步（启用+额度）→ 队列多平台发布 → 掉线拒绝。"""
    # 蚁小二接口是系统级接入（admin 在设置页配置）
    login(client, "root", "pw-root")
    r = client.post("/api/model-config", json={"yxer": {"use_mock": True}})
    assert r.get_json()["ok"] is True

    login(client, "alpha", "pw-alpha")
    client.post("/api/brand", json={"content": "阿尔法珠宝，是杭州的珠宝修复工坊。\n"})

    yx = client.get("/api/yxer-accounts").get_json()
    assert yx["configured"] is True and yx.get("mock") is True
    ids = {a["id"]: a for a in yx["accounts"]}
    assert "yx-souhu-1" in ids and ids["yx-xiaohongshu-1"]["online"] is False

    # 写两篇终稿
    assert client.post("/api/run", json={"count": 2}).get_json()["ok"] is True
    _poll(client, "/api/status", lambda d: not d["running"])
    dirs = [a["dir"] for a in client.get("/api/articles").get_json()["articles"]]

    # 勾选同步：两个搜狐号 + 一个百家号（各设额度），小红书与抖音号不勾（归图文产线）
    items = [{"id": i, "name": ids[i]["name"], "platform": ids[i]["platform"], "limit": 5}
             for i in ("yx-souhu-1", "yx-souhu-2", "yx-baijiahao-1")]
    sync = client.post("/api/yxer-accounts/sync", json={"items": items}).get_json()
    assert sync["ok"] is True and sync["count"] == 3

    # 专用通道卡过滤 yxer 账号（auth.html acLoad 语义）；my-accounts 里可见 yxer_id
    mine = client.get("/api/my-accounts").get_json()["accounts"]
    assert len(mine) == 3 and all(a["yxer_id"] for a in mine)

    # 渠道池出现蚁小二账号
    chs = client.get("/api/client/channels").get_json()["channels"]
    assert sum(1 for c in chs if c["id"].startswith("yx-")) == 3

    # 队列发布：一篇 × 三个勾选账号（蚁小二分发，mock 确定性链接）
    q = client.post("/api/client/queue-publish",
                    json={"dirs": [dirs[0]], "accounts": [a["id"] for a in mine]}).get_json()
    assert q["ok"] is True and q["queued"] == 3
    import time as _t
    deadline = _t.time() + 30
    while _t.time() < deadline:
        tk = client.get("/api/tasks").get_json()
        if not tk["running"]:
            break
        _t.sleep(0.2)
    assert len(tk["tasks"]) == 3 and all(t["status"] == "success" for t in tk["tasks"])
    assert all("mock-yxer.example.com" in t["url"] for t in tk["tasks"])
    arts = client.get("/api/articles").get_json()["articles"]
    a0 = next(a for a in arts if a["dir"] == dirs[0])
    assert a0["published"] is True and {e["platform"] for e in a0["publish_entries"]} == {"souhu", "baijiahao"}

    # 取消勾选 = 停用（不删除）；发布日志计数
    client.post("/api/yxer-accounts/sync", json={"items": items[:2]})
    assert client.get("/api/client/channels").get_json()  # 接口可用
    mine2 = client.get("/api/my-accounts").get_json()["accounts"]
    assert sorted([a["enabled"] for a in mine2]) == [False, True, True]
    assert client.get("/api/publish-log").get_json()["total"] == 3


def test_yxer_offline_publish_fails_honestly(client, app):
    """真实接入未配置：发布经 Yxer 驱动如实失败（人话提示），不影响 fake 渠道。"""
    login(client, "beta", "pw-beta")
    client.post("/api/brand", json={"content": "贝塔科技，是上海的一家技术服务公司。\n"})
    _make_final(app, "yxer未接入文章", tenant=T2)
    # 手工把 beta 的账号标成蚁小二绑定（模拟同步过但配置被清除）
    with app.app_context():
        with tenant_scope(T2):
            db.session.add(Account(id="yx-orphan", tenant_id=T2, platform="souhu",
                                   name="孤儿云号", daily_limit=5, used_today=0,
                                   quota_date=date.today(), yxer_id="yx-orphan"))
            db.session.commit()
    out = client.post("/api/publish-one",
                      json={"dir": "yxer未接入文章", "account_id": "yx-orphan"}).get_json()
    assert out["ok"] is False and "蚁小二" in out["error"]


def test_yxer_config_validation(client):
    login(client, "root", "pw-root")
    bad = client.post("/api/model-config", json={"yxer": {"base_url": "ftp://x", "token": "t"}})
    assert bad.status_code == 400
    bad = client.post("/api/model-config", json={"yxer": {"base_url": "", "token": ""}})
    assert bad.status_code == 400 and "演示" in bad.get_json()["message"]
    ok = client.post("/api/model-config", json={"yxer": {
        "base_url": "https://open.example.com/api", "token": "tok-1234567890"}}).get_json()
    assert ok["ok"] is True
    got = client.get("/api/model-config").get_json()["yxer"]
    assert got["configured"] is True and got["has_token"] is True
    assert "tok-1234567890" not in json.dumps(got)
    # test-yxer 用表单现值真实外呼 → 失败路径
    t = client.post("/api/model-config/test-yxer", json={}).get_json()
    assert t["ok"] is False and "失败" in t["error"]


# ── 抖音图文产线（LLM 文案 + 演示生图 + 蚁小二发布）───────────────────────

def _dy_setup(client):
    """配产线 + 开蚁小二演示云号。"""
    login(client, "root", "pw-root")
    client.post("/api/model-config", json={"yxer": {"use_mock": True}})
    login(client, "alpha", "pw-alpha")
    r = client.post("/api/douyin/config", json={
        "cities": ["杭州", "苏州"], "phone": "13800000000",
        "style_prompt": "竖版 9:16 赛博朋克霓虹科技风",
        "image_count_min": 2, "image_count_max": 2,
        "banned_words": ["先检查后报价"],
        "schedule": [{"time": "09:00", "count": 2, "enabled": True}]})
    assert r.get_json()["ok"] is True
    # 画风必须真的落库并读回（兜底池下线后，画风是唯一的出图风格入口）
    assert client.get("/api/douyin/config").get_json()["style_prompt"].startswith("竖版 9:16")


def _wait_dy(client, path, pred, timeout=40):
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        d = client.get(path).get_json()
        if pred(d):
            return d
        _t.sleep(0.2)
    return d


def test_douyin_config_roundtrip(client):
    login(client, "alpha", "pw-alpha")
    cfg = client.get("/api/douyin/config").get_json()
    assert cfg["image_count_min"] == 4 and cfg["image_count_max"] == 6   # 出厂默认
    assert cfg["_resolved"]["brand"] == "阿尔法珠宝"                       # 品牌自动解析
    assert cfg["_resolved"]["cities"] == ["杭州", "上海"]                  # 回落 TitleConfig
    provs = client.get("/api/douyin/image-providers").get_json()
    assert {p["id"] for p in provs["providers"]} == {"mock"}
    assert provs["current"] == "mock"
    # 张数区间非法 → 400
    assert client.post("/api/douyin/config", json={
        "image_count_min": 8, "image_count_max": 3}).status_code == 400


def test_douyin_generate_edit_delete(client):
    _dy_setup(client)
    r = client.post("/api/douyin/generate", json={"count": 2})
    assert r.get_json() == {"ok": True, "queued": 2}
    st = _wait_dy(client, "/api/douyin/status", lambda d: not d["gen"]["running"])
    assert st["gen"]["pending"] == 0
    assert sum("✅" in l for l in st["gen_logs"]) == 2

    posts = client.get("/api/douyin/posts").get_json()
    assert len(posts) == 2
    p = posts[0]
    assert p["city"] in ("杭州", "苏州") and p["phone"] == "13800000000"
    assert len(p["images"]) == 2 and p["images"][0].startswith("/media/douyin/")
    # 2026-10-19：假供应商现在会返回合法 JSON 文案（否则"兜底不进配图"会拦下生成），
    # 所以这条包是真文案、qc 为空。兜底/违禁词的覆盖在 test_douyin_copy。
    assert p["qc"] == []
    img = client.get(p["images"][0])
    assert img.status_code == 200 and img.mimetype == "image/svg+xml"

    # 编辑：改标题 + 保留 1 图
    keep = p["images"][0].split("/")[-1]
    r = client.post("/api/douyin/pack/save", json={
        "pack_id": p["id"], "title": "改过的标题", "description": "新文案",
        "tags": "同城, 上门", "images": [keep]}).get_json()
    assert r["ok"] is True
    posts = client.get("/api/douyin/posts").get_json()
    edited = next(x for x in posts if x["id"] == p["id"])
    assert edited["title"] == "改过的标题" and edited["tags"] == ["同城", "上门"]
    assert edited["images"] == [f"/media/douyin/{p['id']}/{keep}"]

    assert client.post("/api/douyin/pack/delete", json={"pack_id": p["id"]}).get_json()["ok"] is True
    assert all(x["id"] != p["id"] for x in client.get("/api/douyin/posts").get_json())
    # 已删包的图片 404
    assert client.get(f"/media/douyin/{p['id']}/{keep}").status_code == 404


def test_douyin_timed_publish_roundtrip(client):
    """每条作品的定时发布：编辑弹窗里存时刻 → 内容包列表带出来 → 可改可撤。

    校验失败的**不回写任何字段**：同一次提交里还有标题/文案，时间写错不该把它们
    一起丢掉（用户的编辑成果比时间格式更贵）。
    """
    _dy_setup(client)
    client.post("/api/douyin/generate", json={"count": 1})
    _wait_dy(client, "/api/douyin/status", lambda d: not d["gen"]["running"])
    p = client.get("/api/douyin/posts").get_json()[0]
    assert p["publish_at"] == "" and p["publish_state"] == ""   # 默认没排期

    r = client.post("/api/douyin/pack/save", json={
        "pack_id": p["id"], "title": "定时标题", "publish_at": "2026-10-20 12:25"}).get_json()
    assert r == {"ok": True, "publish_at": "2026-10-20 12:25"}
    got = next(x for x in client.get("/api/douyin/posts").get_json() if x["id"] == p["id"])
    assert got["title"] == "定时标题" and got["publish_at"] == "2026-10-20 12:25"
    assert got["publish_state"] == "pending"

    # 时间写坏 → ok:false + 原因；标题**保持上一次的值**（这次提交整体不落盘）
    bad = client.post("/api/douyin/pack/save", json={
        "pack_id": p["id"], "title": "不该写进去", "publish_at": "20号中午"}).get_json()
    assert bad["ok"] is False and "无法识别" in bad["error"]
    got = next(x for x in client.get("/api/douyin/posts").get_json() if x["id"] == p["id"])
    assert got["title"] == "定时标题" and got["publish_at"] == "2026-10-20 12:25"

    # 留空 = 撤销定时
    r = client.post("/api/douyin/pack/save", json={
        "pack_id": p["id"], "publish_at": ""}).get_json()
    assert r == {"ok": True, "publish_at": ""}
    got = next(x for x in client.get("/api/douyin/posts").get_json() if x["id"] == p["id"])
    assert got["publish_at"] == "" and got["publish_state"] == ""


def test_douyin_pack_save_keeps_long_copy_and_list_tags(client):
    """编辑保存**不得**砍正文：产线规格是 600–900 字，而这里曾被截到 255 字。

    2026-10-19 实测：把一篇 796 字的稿子写进去，落盘只剩 255 字（连固定尾行都被
    砍掉），页面照样提示保存成功 —— 用户正文就这么没了。另：`tags` 传数组时以前
    会变成 `"['a'"` 这种碎片（`str(list).split(",")`），现在按数组收。
    """
    _dy_setup(client)
    client.post("/api/douyin/generate", json={"count": 1})
    _wait_dy(client, "/api/douyin/status", lambda d: not d["gen"]["running"])
    pid = client.get("/api/douyin/posts").get_json()[0]["id"]

    body = "断镯知识" * 140                      # 560 字
    assert client.post("/api/douyin/pack/save", json={
        "pack_id": pid, "description": body,
        "tags": ["玉镯修复", "断镯修复"]}).get_json()["ok"] is True
    got = next(x for x in client.get("/api/douyin/posts").get_json() if x["id"] == pid)
    assert got["description"] == body, "正文不能被截断"
    assert got["tags"] == ["玉镯修复", "断镯修复"], "数组形式的 tags 要能用"


def test_douyin_publish_via_yxer(client):
    _dy_setup(client)
    client.post("/api/douyin/generate", json={"count": 1})
    _wait_dy(client, "/api/douyin/status", lambda d: not d["gen"]["running"])
    pid = client.get("/api/douyin/posts").get_json()[0]["id"]

    accts = client.get("/api/douyin/accounts").get_json()
    dy = [a for a in accts if str(a["id"]).startswith("yx-") and a["status"] == 1]
    assert [a["id"] for a in dy] == ["yx-yx-douyin-1", "yx-yx-douyin-2"]

    r = client.post("/api/douyin/publish", json={"pack_ids": [pid], "account_ids": ["yx-yx-douyin-1"]})
    assert r.get_json()["ok"] is True
    st = _wait_dy(client, "/api/douyin/status", lambda d: not d["pub"]["active"])
    assert st["pub"]["done"] == 1 and any("✅" in l for l in st["pub_logs"])

    led = client.get("/api/douyin/ledger").get_json()
    assert led[str(pid)][0]["ok"] is True and "mock-yxer" in led[str(pid)][0]["url"]

    # 未知账号拒绝
    assert client.post("/api/douyin/publish", json={
        "pack_ids": [pid], "account_ids": ["ghost"]}).get_json()["ok"] is False


def test_douyin_publish_reports_honest_count(client):
    """页面报的条数 = 实际会发的条数，且两种分发口径都如实回报。

    - `each`（默认）：每个勾选的号**各发一份** → 2 包 × 2 号 = 4 条；
    - `rotate`：一包只落一个号 → 2 包 × 2 号 = 2 条（同一篇不重复铺）。
    弹窗里的"将发出 N 条"走 /api/douyin/publish/preview，与真正执行共用同一份
    `douyin_publish.plan` —— 叉乘时代页面显示 4 条、实际发 2 条；2026-10-19 又
    反过来"勾了所有号只发 1 条"。两边同源，两种口径分别验。
    """
    _dy_setup(client)
    client.post("/api/douyin/generate", json={"count": 2})
    _wait_dy(client, "/api/douyin/status", lambda d: not d["gen"]["running"])
    pids = [p["id"] for p in client.get("/api/douyin/posts").get_json()][:2]
    accts = ["yx-yx-douyin-1", "yx-yx-douyin-2"]

    # 预览（默认 each）+ 不认识的 mode 退回默认，绝不静默按别的口径发
    pv = client.post("/api/douyin/publish/preview",
                     json={"pack_ids": pids, "account_ids": accts}).get_json()
    assert pv["mode"] == "each" and pv["queued"] == 4
    pv = client.post("/api/douyin/publish/preview",
                     json={"pack_ids": pids, "account_ids": accts, "mode": "rotate"}).get_json()
    assert pv["mode"] == "rotate" and pv["queued"] == 2
    pv = client.post("/api/douyin/publish/preview",
                     json={"pack_ids": pids, "account_ids": accts, "mode": "??"}).get_json()
    assert pv["mode"] == "each"

    r = client.post("/api/douyin/publish", json={"pack_ids": pids, "account_ids": accts,
                                                 "mode": "each"}).get_json()
    assert r["queued"] == len(pids) * len(accts), "each：一包对每个勾选的号各一份"
    st = _wait_dy(client, "/api/douyin/status", lambda d: not d["pub"]["active"])
    assert st["pub"]["total"] == len(pids) * len(accts)
    led = client.get("/api/douyin/ledger").get_json()
    for pid in pids:
        assert sorted(x["account_id"] for x in led[str(pid)]) == sorted(accts)

    # each 补发：同一批再发一次 → 每个号都已发过，0 条且如实给出去向说明
    again = client.post("/api/douyin/publish", json={"pack_ids": pids, "account_ids": accts,
                                                     "mode": "each"}).get_json()
    assert again["queued"] == 0 and again["notes"]

    # rotate：同一批换口径重发，一个包只占一个号
    client.post("/api/douyin/generate", json={"count": 2})
    _wait_dy(client, "/api/douyin/status", lambda d: not d["gen"]["running"])
    have = {str(x) for x in pids}
    fresh_pids = [p["id"] for p in client.get("/api/douyin/posts").get_json()
                  if str(p["id"]) not in have][:2]
    assert len(fresh_pids) == 2
    r = client.post("/api/douyin/publish", json={"pack_ids": fresh_pids, "account_ids": accts,
                                                 "mode": "rotate"}).get_json()
    assert r["queued"] == len(fresh_pids), "rotate：一包只发一个号"
    _wait_dy(client, "/api/douyin/status", lambda d: not d["pub"]["active"])
    led = client.get("/api/douyin/ledger").get_json()
    for pid in fresh_pids:
        assert len(led[str(pid)]) == 1


def test_douyin_guardrails(client):
    login(client, "beta", "pw-beta")
    # 无城市 → 拒（beta 无 TitleConfig）
    r = client.post("/api/douyin/generate", json={"count": 1})
    assert r.status_code == 400 and "城市" in r.get_json()["error"]

    # autopub 校验 + 回读
    assert client.post("/api/douyin/autopub", json={"enabled": True, "account_ids": []}).status_code == 400
    client.post("/api/douyin/autopub", json={"enabled": True, "account_ids": ["yx-dy-9"],
                                             "interval_min": 45, "gen_mode": "interval"})
    ap = client.get("/api/douyin/autopub").get_json()["config"]
    assert ap["interval_min"] == 45 and ap["gen_mode"] == "interval" and ap["enabled"] is True


# ── 在线模型配置（任意 OpenAI 兼容端点）───────────────────────────────────

def test_model_config_get_shape(client):
    login(client, "root", "pw-root")
    body = client.get("/api/model-config").get_json()
    assert body["ok"] is True
    assert body["llm"]["provider"] == "fake" and body["llm"]["source"] == "env"
    # 测试环境 GEO_LLM_KEYS 有值 → 掩码回显（绝不明文）；生图未配置
    assert body["llm"]["has_key"] is True and body["image"]["configured"] is False
    assert "k-alpha" not in json.dumps(body)


def test_model_config_save_affects_generation(client, app):
    """在线保存任意兼容端点 → 生效链改变（模型标签 + build_llm_client 装配）。"""
    login(client, "root", "pw-root")
    r = client.post("/api/model-config", json={"llm": {
        "provider": "openai", "base_url": "https://gw.example.com/v1",
        "model": "my-any-model", "key": "sk-abcdefghijklmnop"}})
    assert r.get_json()["ok"] is True

    got = client.get("/api/model-config").get_json()
    assert got["llm"]["source"] == "online"
    assert got["llm"]["key_masked"] == "sk-***mnop"        # 掩码回显，绝不明文
    assert "abcdefghijklmnop" not in json.dumps(got)

    assert client.get("/api/config").get_json()["ai"]["model"] == "my-any-model"

    from app.services.llm import build_llm_client
    with app.app_context():
        cli = build_llm_client(app.config["GEO_SETTINGS"])
        assert cli.provider.base_url == "https://gw.example.com/v1"
        assert cli.provider.model == "my-any-model"

    # key 留空 = 保持已存
    client.post("/api/model-config", json={"llm": {
        "provider": "openai", "base_url": "https://gw.example.com/v1",
        "model": "other-model", "key": ""}})
    assert client.get("/api/model-config").get_json()["llm"]["key_masked"] == "sk-***mnop"

    assert client.post("/api/model-config/reset", json={}).get_json()["ok"] is True
    assert client.get("/api/model-config").get_json()["llm"]["source"] == "env"


def test_model_config_validation_and_guard(client):
    login(client, "root", "pw-root")
    bad = client.post("/api/model-config", json={"llm": {"provider": "openai", "key": "k"}})
    assert bad.status_code == 400 and "base_url" in bad.get_json()["message"]
    assert client.post("/api/model-config", json={"llm": {
        "provider": "openai", "base_url": "ftp://x", "key": "k"}}).status_code == 400
    assert client.post("/api/model-config", json={"image": {
        "base_url": "https://x", "model": ""}}).status_code == 400

    login(client, "alpha", "pw-alpha")
    assert client.post("/api/model-config", json={"llm": {"provider": "fake"}}).status_code == 403
    assert client.post("/api/model-config/test-llm", json={}).status_code == 403
    # 客户可读（顶栏标签要用 /api/config），不可写


def test_model_config_test_llm(client):
    login(client, "root", "pw-root")
    client.post("/api/model-config", json={"llm": {"provider": "fake"}})
    body = client.post("/api/model-config/test-llm", json={"provider": "fake"}).get_json()
    assert body["ok"] is True and "fake" in body["msg"]
    body = client.post("/api/model-config/test-llm", json={
        "provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "x",
        "key": "sk-test"}).get_json()
    assert body["ok"] is False and "连接失败" in body["error"]


# ── 账号授权（auth.html 专用通道卡）───────────────────────────────────────

def test_accounts_full_cycle_with_publish(client, app, monkeypatch):
    """add → limit/cookie/verify/toggle → channels 出现 → publish-one 闭环 → delete。"""
    login(client, "alpha", "pw-alpha")
    _make_final(app, "账号闭环文章")

    r = client.post("/api/my-accounts/add",
                    json={"platform": "meipian", "name": "美篇主号", "limit": 3}).get_json()
    assert r["ok"] is True and r["id"].startswith("mp-")
    acc_id = r["id"]

    lst = client.get("/api/my-accounts").get_json()["accounts"]
    assert len(lst) == 1
    a = lst[0]
    assert a["name"] == "美篇主号" and a["limit"] == 3 and a["enabled"] is True
    assert a["verified"] is False and a["has_cookie"] is False

    assert client.post("/api/my-accounts/cookie",
                       json={"id": acc_id, "cookie": "lieju_passport=abc; x=1"}).get_json()["ok"] is True
    assert client.get("/api/my-accounts").get_json()["accounts"][0]["has_cookie"] is True
    with app.app_context():
        from app.models import Account
        from app.services.crypto import decrypt_text
        with tenant_scope(T1):
            blob = db.session.get(Account, acc_id).cookie_blob
            assert blob[:8] != b"lieju_pa"          # 密文入库（规约禁明文）
            assert decrypt_text(blob) == "lieju_passport=abc; x=1"

    # verify 现在是**真实验证**（会真起浏览器访问平台）。测试不联网，
    # 用替身返回"通过"——这里要验的是"校验通过后 verified 被置位"的链路。
    from app.services.publish import login as qr_login

    monkeypatch.setattr(qr_login, "verify_cookie",
                        lambda acc: {"ok": True, "msg": "登录态校验通过"})

    v = client.post("/api/my-accounts/verify", json={"id": acc_id}).get_json()
    assert v["ok"] is True and "校验通过" in v["msg"]
    assert client.get("/api/my-accounts").get_json()["accounts"][0]["verified"] is True

    chs = client.get("/api/client/channels").get_json()["channels"]
    assert [c["id"] for c in chs] == [acc_id, "tenant-all"] and chs[0]["left"] == 3

    out = client.post("/api/publish-one", json={"dir": "账号闭环文章", "account_id": acc_id}).get_json()
    assert out["ok"] is True and out["url"]
    assert client.get("/api/my-accounts").get_json()["accounts"][0]["used_today"] == 1

    assert client.post("/api/my-accounts/toggle", json={"id": acc_id}).get_json()["enabled"] is False
    assert client.get("/api/client/channels").get_json()["channels"] == []   # 停用即退出渠道池
    client.post("/api/my-accounts/toggle", json={"id": acc_id})

    assert client.post("/api/my-accounts/limit", json={"id": acc_id, "limit": 0}).get_json()["ok"] is True
    assert client.get("/api/client/channels").get_json()["channels"] == []   # 额度 0 = 登记型排除

    assert client.post("/api/my-accounts/delete", json={"id": acc_id}).get_json() == {"ok": True}
    assert client.get("/api/my-accounts").get_json()["accounts"] == []


def test_accounts_validation(client):
    login(client, "alpha", "pw-alpha")
    assert client.post("/api/my-accounts/add", json={"name": ""}).status_code == 400
    assert client.post("/api/my-accounts/add",
                       json={"platform": "tiktok", "name": "x"}).status_code == 400
    assert client.post("/api/my-accounts/verify", json={"id": "no-such"}).status_code == 404
    assert client.post("/api/my-accounts/limit",
                       json={"id": "no-such", "limit": 5}).status_code == 404


# ── 定时调度执行器（tick 语义：到期才触发）─────────────────────────────────

def _tick(app):
    """调度扫描必须在 app context（db.session + LLM settings 都挂在其上）。"""
    from app.services import scheduler
    with app.app_context():
        return scheduler.tick()


def _poll(client, url, pred, timeout=30.0):
    import time as _t

    deadline = _t.time() + timeout
    body = None
    while _t.time() < deadline:
        body = client.get(url).get_json()
        if body is not None and pred(body):
            return body
        _t.sleep(0.1)
    return body


def _set_schedule(app, tenant, kind, **fields):
    from app.models import Schedule

    with app.app_context():
        with tenant_scope(tenant):
            repo = TenantScopedRepository(Schedule)
            row = repo.first(kind=kind)
            if row is None:
                row = repo.add(Schedule(kind=kind, mode="daily", enabled=False))
            for k, v in fields.items():
                setattr(row, k, v)
            db.session.commit()


def _get_schedule(app, tenant, kind):
    from app.models import Schedule
    from app.services.scheduler import _aware

    with app.app_context():
        with tenant_scope(tenant):
            row = TenantScopedRepository(Schedule).first(kind=kind)
            if row is None:
                return None
            # _aware：sqlite 读回是裸值，统一补上海时区后再与 now_local() 比较
            return {"enabled": row.enabled, "next_run_at": _aware(row.next_run_at),
                    "config": row.config}


def test_scheduler_title_plan_due_only(client, app):
    from datetime import timedelta

    from app.services import scheduler
    login(client, "alpha", "pw-alpha")
    assert client.post("/api/autosched", json={"title_sched": {
        "enabled": True, "mode": "interval", "every_min": 30, "count": 5}}).get_json() == {"ok": True}

    # 第一轮：next_run_at 缺失 → 只初始化，不触发（保存计划≠马上执行）
    out = _tick(app)
    assert out["fired"] == [] and out["skipped"] == []
    s = _get_schedule(app, T1, "title")
    assert s["next_run_at"] is not None and s["next_run_at"] > now_local()

    # 拨到过去 → 触发
    _set_schedule(app, T1, "title", next_run_at=now_local() - timedelta(minutes=1))
    out = _tick(app)
    assert any(":title" in f for f in out["fired"])

    st = _poll(client, "/api/titles/generate-status", lambda d: not d["running"])
    assert st["error"] is None and len(st["titles"]) >= 1
    # 触发后 next_run_at 已推进到未来
    s2 = _get_schedule(app, T1, "title")
    assert s2["next_run_at"] > now_local()


def test_scheduler_article_plan(client, app):
    from datetime import timedelta

    from app.services import scheduler
    login(client, "alpha", "pw-alpha")
    _set_schedule(app, T1, "article", enabled=True, mode="interval",
                  every_min=30, count=1, next_run_at=now_local() - timedelta(minutes=1))
    out = _tick(app)
    assert any(":article" in f for f in out["fired"])
    s = _poll(client, "/api/status", lambda d: not d["running"])
    assert s["progress"]["success"] == 1
    assert client.get("/api/articles").get_json()["total"] == 1


def test_scheduler_article_plan_blocked_without_brand(client, app):
    """品牌缺失 → begin_run 拒绝，调度记 skipped 且仍推进 next_run_at（不狂重试）。"""
    from datetime import timedelta

    from app.services import scheduler
    login(client, "beta", "pw-beta")
    client.post("/api/brand", json={"content": ""})
    _set_schedule(app, T2, "article", enabled=True, mode="interval",
                  every_min=30, count=1, next_run_at=now_local() - timedelta(minutes=1))
    out = _tick(app)
    assert any("beta:article" in sk for sk in out["skipped"])
    s = _get_schedule(app, T2, "article")
    assert s["next_run_at"] > now_local()


def test_scheduler_publish_plan(client, app):
    import json as _json
    from datetime import timedelta

    from app.services import scheduler
    login(client, "alpha", "pw-alpha")
    _add_account(app, "mp-1", limit=5)
    _make_final(app, "调度发布样本")
    plans = [{"id": 1, "channel": "meipian", "account_id": "mp-1", "account_name": "账号mp-1",
              "count": 5, "time": "08:30", "mode": "interval", "every_min": 30, "enabled": True}]
    _set_schedule(app, T1, "publish", enabled=True, mode="interval", every_min=30,
                  count=5, config=_json.dumps(plans),
                  next_run_at=now_local() - timedelta(minutes=1))
    out = _tick(app)
    assert any(":publish:1" in f for f in out["fired"])
    import time as _t
    deadline = _t.time() + 30
    while _t.time() < deadline:
        tk = client.get("/api/tasks").get_json()
        if not tk["running"]:
            break
        _t.sleep(0.2)
    assert tk["tasks"][0]["status"] == "success"
    assert client.get("/api/articles").get_json()["articles"][0]["published"] is True


def test_assets_flow_into_generation(client, app):
    """素材随机取用（原站规则：文章自动配图）；删空后不再注入。"""
    import io

    from app.services.writer.pipeline import pick_image_html

    login(client, "alpha", "pw-alpha")
    import base64

    # 真实 1×1 PNG：上传已加"文件头魔数"校验，假扩展名不再进素材库
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
    client.post("/api/my-assets", data={"files": (io.BytesIO(png), "配图.png")},
                content_type="multipart/form-data")
    with app.app_context():
        with tenant_scope(T1):
            assert "/media/assets/" in pick_image_html()

    aid = client.get("/api/my-assets").get_json()["assets"][0]["id"]
    client.post("/api/my-assets/delete", json={"id": aid})
    with app.app_context():
        with tenant_scope(T1):
            assert pick_image_html() == ""
