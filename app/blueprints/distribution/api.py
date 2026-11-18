"""distribution 蓝图 · JSON 接口（同 views 的职责，返回 JSON）。"""
from __future__ import annotations

from flask import jsonify, request

from app.blueprints.distribution import bp
from app.blueprints.distribution.accounts_service import AccountService
from app.blueprints.distribution.service import DistributionService
from app.blueprints.distribution.task_service import TaskService
from app.blueprints.distribution.yxer_service import YxerAccountService


def _payload() -> dict:
    return request.get_json(silent=True) or {}


@bp.get("/api/distribution/ping", endpoint="distribution_ping")
def distribution_ping():
    return jsonify({"ok": True, "blueprint": "distribution"})


# ── 文章档案（archives.html）────────────────────────────────────────────────

@bp.get("/api/articles", endpoint="articles_api")
def articles_api():
    return jsonify(DistributionService().list_articles(request.args.get("limit", 2000)))


@bp.get("/api/article/<dir_name>/<filename>", endpoint="article_file_api")
def article_file_api(dir_name, filename):
    return jsonify(DistributionService().read_file(dir_name, filename))


@bp.get("/api/publish-log", endpoint="publish_log_api")
def publish_log_api():
    return jsonify(DistributionService().publish_log(request.args.get("limit", 5000)))


# ── 发布渠道（channels/preflight/queue-publish）─────────────────────────────

@bp.get("/api/client/channels", endpoint="client_channels_api")
def client_channels_api():
    return jsonify(DistributionService().list_channels())


@bp.get("/api/client/preflight", endpoint="preflight_api")
def preflight_api():
    return jsonify(TaskService().preflight())


@bp.post("/api/client/queue-publish", endpoint="queue_publish_api")
def queue_publish_api():
    return jsonify(TaskService().queue_publish(_payload()))


# ── 任务中心（tasks.html）───────────────────────────────────────────────────

@bp.get("/api/tasks", endpoint="tasks_api")
def tasks_api():
    return jsonify(TaskService().list_tasks())


@bp.post("/api/tasks/stop", endpoint="tasks_stop_api")
def tasks_stop_api():
    return jsonify(TaskService().stop())


@bp.post("/api/tasks/run", endpoint="tasks_resume_api")
def tasks_resume_api():
    return jsonify(TaskService().resume())


# ── 单篇发布（archives.html doPub）──────────────────────────────────────────

@bp.get("/api/publish-pick", endpoint="publish_pick_api")
def publish_pick_api():
    return jsonify(TaskService().pick_account())


@bp.post("/api/publish-one", endpoint="publish_one_api")
def publish_one_api():
    body = _payload()
    return jsonify(TaskService().publish_one(str(body.get("dir") or ""),
                                             str(body.get("account_id") or "")))


@bp.post("/api/publish/sync", endpoint="publish_sync_api")
def publish_sync_api():
    """回查蚁小二的发布结果 —— 提交成功 ≠ 已发布（异步任务要单独确认）。"""
    from app.services.publish import status_sync

    return jsonify({"ok": True, **status_sync.sync_pending()})


# ── 账号授权（auth.html「专用通道」卡）─────────────────────────────────────

@bp.get("/api/my-accounts", endpoint="my_accounts_api")
def my_accounts_api():
    return jsonify(AccountService().list_accounts())


@bp.post("/api/my-accounts/add", endpoint="my_accounts_add_api")
def my_accounts_add_api():
    return jsonify(AccountService().add(_payload()))


@bp.post("/api/my-accounts/limit", endpoint="my_accounts_limit_api")
def my_accounts_limit_api():
    body = _payload()
    return jsonify(AccountService().set_limit(body.get("id"), body.get("limit")))


@bp.post("/api/my-accounts/cookie", endpoint="my_accounts_cookie_api")
def my_accounts_cookie_api():
    body = _payload()
    return jsonify(AccountService().set_cookie(body.get("id"), body.get("cookie")))


@bp.post("/api/my-accounts/verify", endpoint="my_accounts_verify_api")
def my_accounts_verify_api():
    return jsonify(AccountService().verify(_payload().get("id")))


# ── 扫码登录（专用通道：美篇/列表网/58/搜狐/百家号）──────────────────────────

@bp.post("/api/my-accounts/login", endpoint="my_accounts_login_api")
def my_accounts_login_api():
    """打开一个可见浏览器让人扫码；返回 login_id 供前端轮询。"""
    from app.services.publish import login as qr_login
    from app.services.publish.drivers import PublishError

    row = AccountService().get_for_login(str(_payload().get("id") or ""))
    try:
        login_id = qr_login.start_login(row)
    except PublishError as exc:
        return jsonify({"ok": False, "error": exc.message}), 400
    return jsonify({"ok": True, "login_id": login_id})


@bp.get("/api/my-accounts/login-status", endpoint="my_accounts_login_status_api")
def my_accounts_login_status_api():
    from app.services.publish import login as qr_login

    return jsonify(qr_login.login_status(request.args.get("login_id", "")))


@bp.post("/api/my-accounts/login-confirm", endpoint="my_accounts_login_confirm_api")
def my_accounts_login_confirm_api():
    """手动确认（自动判定拿不准时，用户点"我扫完了"）。"""
    from app.services.publish import login as qr_login

    return jsonify(qr_login.confirm_login(_payload().get("login_id")))


@bp.post("/api/my-accounts/login-cancel", endpoint="my_accounts_login_cancel_api")
def my_accounts_login_cancel_api():
    from app.services.publish import login as qr_login

    return jsonify(qr_login.cancel_login(_payload().get("login_id")))


@bp.post("/api/my-accounts/toggle", endpoint="my_accounts_toggle_api")
def my_accounts_toggle_api():
    return jsonify(AccountService().toggle(_payload().get("id")))


@bp.post("/api/my-accounts/delete", endpoint="my_accounts_delete_api")
def my_accounts_delete_api():
    return jsonify(AccountService().delete(_payload().get("id")))


# ── 蚁小二账号库（auth.html「蚁小二账号库」卡）─────────────────────────────

@bp.get("/api/yxer-accounts", endpoint="yxer_accounts_api")
def yxer_accounts_api():
    return jsonify(YxerAccountService().cloud_accounts())


@bp.post("/api/yxer-accounts/sync", endpoint="yxer_accounts_sync_api")
def yxer_accounts_sync_api():
    return jsonify(YxerAccountService().sync(_payload()))
