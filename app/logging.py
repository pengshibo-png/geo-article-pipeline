"""结构化 JSON 日志 + request_id / tenant_id 透传（ADR-010）。

- request_id：每个请求生成，也允许上游通过 X-Request-Id 传入，便于跨服务串联。
- tenant_id：由 tenancy 层写入 contextvar，日志自动带上（不侵入业务代码）。
- 两个 contextvar 都用 default=None，未绑定时输出 "-"，避免日志缺字段。
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("log_tenant_id", default=None)

# LogRecord 的内置属性（非 extra）：用来把「业务 extra」与标准字段分开。
# 用实例 __dict__ 生成，避免手写遗漏（不同 Python 版本内置属性有增减）。
_STD_KEYS = frozenset(logging.LogRecord(
    "", logging.NOTSET, "", 0, "", (), None).__dict__.keys()) | {"message", "asctime"}


class _ContextFilter(logging.Filter):
    """把 contextvar 与业务 extra 归一进 record，供 Formatter 使用。

    之前只放行 7 个白名单键（method/path/status/duration_ms/event/actor/reason），
    业务代码用的 tenant/err/engine/count/task… 全被**静默丢弃** —— 出错时日志只剩
    一句 msg，看不出原因（例：douyin_autopub_failed 的 err 就是这么丢的）。
    现改为「除 LogRecord 内置属性外的所有键都算 extra」，不再漏字段。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.tenant_id = tenant_id_var.get() or "-"
        record.extra_fields = {
            key: value for key, value in record.__dict__.items()
            if key not in _STD_KEYS and key not in ("request_id", "tenant_id")}
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "tenant_id": getattr(record, "tenant_id", "-"),
        }
        payload.update(getattr(record, "extra_fields", None) or {})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s [%(request_id)s|%(tenant_id)s] %(name)s: %(message)s")


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_ContextFilter())
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_request_id(rid: str | None = None) -> str:
    rid = rid or new_request_id()
    request_id_var.set(rid)
    return rid


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
