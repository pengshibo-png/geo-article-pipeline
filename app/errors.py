"""类型化错误层级 + 全局错误处理器。

约定：
- 业务代码只 `raise` 语义化错误，不自己拼 HTTP 状态。
- `/api/*` 返回 JSON，页面路由返回简洁 HTML（不泄露内部细节）。
- **越权读取返回 404 而非 403**（V4 契约，避免存在性泄漏）。
"""
from __future__ import annotations

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from app.logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    """所有业务异常基类。"""

    code = "internal_error"
    http_status = 500
    message = "内部错误"

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.detail = detail

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {"ok": False, "error": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body


class ValidationError(AppError):
    code, http_status, message = "validation_error", 400, "参数校验失败"


class Unauthorized(AppError):
    code, http_status, message = "unauthorized", 401, "未登录或会话已失效"


class Forbidden(AppError):
    code, http_status, message = "forbidden", 403, "无权访问"


class NotFound(AppError):
    code, http_status, message = "not_found", 404, "资源不存在"


class RateLimited(AppError):
    code, http_status, message = "rate_limited", 429, "请求过于频繁，请稍后再试"


class TenantViolation(AppError):
    """写侧跨租户越权（V3）。默认 403；读侧越权请用 NotFound（V4）。"""

    code, http_status, message = "tenant_violation", 403, "跨租户操作被拒绝"


class RewriteRequired(ValidationError):
    """B 段判定「建议重写」：C 段按冻结规则**不得**代改这类稿子。

    这是个**类型化信号**而不是终点失败 —— 编排层（writer/rewrite.py）捕获它，
    回 A 段重写一篇新的，并把上一轮审稿意见注入 A 段 Prompt。
    在此之前它表现为裸 ValidationError，等于"审稿严格 = 整篇作废、零重试"。
    """

    code = "rewrite_required"


class UpstreamError(AppError):
    code, http_status, message = "upstream_error", 502, "上游服务异常"


class ConfigError(AppError):
    code, http_status, message = "config_error", 500, "配置错误"


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(AppError)
    def _handle_app_error(exc: AppError):
        level = log.warning if exc.http_status < 500 else log.error
        level("app_error", extra={"event": "app_error", "status": exc.http_status})
        if _wants_json():
            return jsonify(exc.to_dict()), exc.http_status
        return f"<h1>{exc.http_status}</h1><p>{exc.message}</p>", exc.http_status

    @app.errorhandler(HTTPException)
    def _handle_http(exc: HTTPException):
        if _wants_json():
            return jsonify({"ok": False, "error": exc.name.lower().replace(" ", "_"),
                            "message": exc.description}), exc.code
        return exc

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):
        log.exception("unhandled_exception", extra={"event": "unhandled_exception"})
        if _wants_json():
            return jsonify({"ok": False, "error": "internal_error", "message": "内部错误"}), 500
        return "<h1>500</h1><p>内部错误</p>", 500
