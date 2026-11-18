"""蚁小二发布结果回查的回归测试（2026-10-19 事故）。

事故：8 条发布日志全记 `success=1`，但用户去蚁小二后台一条都没看到。
原因：蚁小二是**异步发布**，`yxer publish` 只返回 `task_set_id`；系统拿到 id 就记成功。
回查后真实结果是百家号 `stageStatus=fail`、
`errorMessage=发布失败您所在网络环境异常，请完成验证`。
"""
from __future__ import annotations


def test_classify_maps_task_state():
    """有 openUrl 才算发布成功；fail 阶段算失败；其余待定（绝不假报成功）。"""
    from app.services.publish.status_sync import classify

    assert classify({"openUrl": "https://baijiahao.baidu.com/s?id=1"})[0] == "ok"

    status, detail, url = classify({
        "stageStatus": "fail", "stages": "push",
        "errorMessage": "发布失败您所在网络环境异常，请完成验证"})
    assert status == "fail" and url == ""
    assert "网络环境异常" in detail

    assert classify({"stageStatus": "publish", "stages": "push"})[0] == "pending"
    # 失败但没给原因时也要有可读说明
    assert "未知" in classify({"stageStatus": "failed"})[1]


def test_clean_open_url_strips_tracking_but_keeps_id():
    """头条/百家号返回的是创作中心跳转链：跟踪参数要剥，文章 id 要留。

    实测数据（2026-10-19）：
      头条   /item/7698.../?enter_from=click_creation_center&category_name=...&gd_ext_json=%7B...
      百家号  ...landingshare?preview=1&pageType=1&...&context=%7B%22nid%22...
    剥掉跟踪参数后仍能定位文章；**不猜测**改成 /article/ 这种"漂亮形态"
    （编出来的链接会 404 得更彻底）。
    """
    from app.services.publish.status_sync import clean_open_url

    toutiao = ("https://www.toutiao.com/item/7000000000000000001/"
               "?enter_from=click_creation_center&category_name=creation_center"
               "&gd_ext_json=%7B%22enter_from%22%3A%22click_creation_center%22%7D")
    cleaned = clean_open_url(toutiao)
    assert cleaned == "https://www.toutiao.com/item/7000000000000000001/"
    assert "enter_from" not in cleaned and "%7B" not in cleaned

    # 百家号的关键参数（preview / context 里的 nid）必须原样保留
    bjh = ("https://mbd.baidu.com/newspage/data/landingshare?preview=1&pageType=1"
           "&context=%7B%22nid%22%3A%22news_10335157152299485953%22%7D")
    kept = clean_open_url(bjh)
    assert "preview=1" in kept and "nid" in kept
    assert kept.startswith("https://mbd.baidu.com/newspage/data/landingshare?")

    # 没 query 的原样返回
    assert clean_open_url("https://www.sohu.com/a/1089626748_122964503") \
        == "https://www.sohu.com/a/1089626748_122964503"


def test_public_url_prefers_platform_article_page():
    """发布链接必须能打开：优先平台原生文章页（头条/百家号/网易）。"""
    from app.services.publish.status_sync import public_url

    tt = "https://www.toutiao.com/item/7000000000000000001/?enter_from=click_creation_center"
    assert public_url("头条号", "7000000000000000001", tt) \
        == "https://www.toutiao.com/article/7000000000000000001/"

    bjh = ("https://mbd.baidu.com/newspage/data/landingshare?preview=1&pageType=1"
           "&context=%7B%22nid%22%3A%22news_10335157152299485953%22%7D")
    assert public_url("百家号", "news_10335157152299485953", bjh) \
        == "https://baijiahao.baidu.com/s?id=10335157152299485953"

    assert public_url("网易号", "1234567890", "x") \
        == "https://www.163.com/dy/article/1234567890.html"

    # 没有 id 或没有规则（如搜狐的 openUrl 本来就能开）→ 退回 openUrl
    assert public_url("头条号", "", tt) == tt
    assert public_url("搜狐号", "", "https://www.sohu.com/a/1089626748_122964503") \
        == "https://www.sohu.com/a/1089626748_122964503"


def test_sync_clears_success_when_remote_failed(app, monkeypatch):
    """回查发现失败 → 必须把 `success` 摘掉并写原因（页面上的绿勾不能撒谎）。"""
    from app.extensions import db
    from app.models import PublishLog
    from app.services.publish import status_sync
    from app.tenancy import tenant_scope
    from app.timeutil import now_local
    from tests.conftest import T1

    with app.app_context(), tenant_scope(T1):
        row = PublishLog(article_dir="某篇文章", title="某篇文章", platform="baijiahao",
                         success=True, url="yxer-task:abc-123", happened_at=now_local())
        db.session.add(row)
        db.session.commit()
        log_id = row.id

    monkeypatch.setattr(status_sync, "query_task", lambda tid: {
        "stageStatus": "fail", "stages": "push",
        "errorMessage": "发布失败您所在网络环境异常，请完成验证"})

    with app.app_context(), tenant_scope(T1):
        counts = status_sync.sync_pending()
        assert counts["fail"] == 1 and counts["checked"] == 1

        r = db.session.get(PublishLog, log_id)
        assert r.success is False, "发布失败必须摘掉 success，否则就是假报成功"
        assert "网络环境异常" in (r.error or "")
        assert r.remote_status == "fail"
        assert r.checked_at is not None


def test_sync_keeps_success_and_stores_real_url(app, monkeypatch):
    """回查成功 → 保留 success，并把真实链接写回 url/remote_url。"""
    from app.extensions import db
    from app.models import PublishLog
    from app.services.publish import status_sync
    from app.tenancy import tenant_scope
    from app.timeutil import now_local
    from tests.conftest import T1

    with app.app_context(), tenant_scope(T1):
        row = PublishLog(article_dir="另一篇", title="另一篇", platform="toutiao",
                         success=True, url="yxer-task:def-456", happened_at=now_local())
        db.session.add(row)
        db.session.commit()
        log_id = row.id

    real = "https://www.toutiao.com/article/123456/"
    monkeypatch.setattr(status_sync, "query_task",
                        lambda tid: {"openUrl": real, "stageStatus": "success"})

    with app.app_context(), tenant_scope(T1):
        counts = status_sync.sync_pending()
        assert counts["ok"] == 1
        r = db.session.get(PublishLog, log_id)
        assert r.success is True
        # 真实链接放 remote_url；url **保留任务 ID** —— 它是下次回查的凭据
        assert r.remote_url == real
        assert r.url == "yxer-task:def-456"
