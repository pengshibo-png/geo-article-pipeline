"""generation 蓝图 · 领域服务（页面驱动的批量写稿编排，gen.html 对接面）。

页面契约（参考系统线上行为实测）：
- POST /api/run {count,no_publish,contact_mode,provider,concurrency,speed} → {ok}|{ok:false,error}
- POST /api/run/pause → {ok}
- GET /api/status → {running,pausing,current,progress,log_total,log_tail,articles}

批量启动的真正逻辑（取题/标 used/品牌预检/池空补货/入队）在
`app/services/writer/managed.begin_run` —— HTTP 入口与调度线程共用一份，
本层只做参数校验与薄委托。进度经进程内状态桥回报（managed.py）。

**如实说明**：provider/concurrency/speed/no_publish 四个参数本期接受但不改变
执行方式 —— 模型走 GEO_LLM_PROVIDER 配置，并发由 gen 队列池大小决定，
发布由发布执行器负责。页面兼容优先，不做假动作。
"""
from __future__ import annotations

from flask import current_app

from app.errors import ValidationError
from app.extensions import db as _db
from app.services.writer import managed

_CONTACT_MODES = ("revise", "strip", "keep")
_MODEL_LABELS = {"fake": "演示模型", "ds-pro": "DeepSeek-V4-Pro", "glm53flash": "GLM-5.3-flash"}


def ai_model_label() -> str:
    """顶栏「模型 · X」标签（layout.js refreshHeader 读 /api/config.ai.model）。

    反映**生效配置**：在线覆盖（系统设置页）优先于环境变量。
    """
    from app.services.model_config import effective_llm

    eff = effective_llm(current_app.config["GEO_SETTINGS"])
    provider = (eff["provider"] or "").lower()
    if provider == "fake":
        return "演示模型"
    if eff["model"]:
        return eff["model"]
    return _MODEL_LABELS.get(provider, provider or "未配置")


class GenService:
    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    def start_run(self, body: dict) -> dict:
        """HTTP 入口：count 严格校验（非法即 400），contact_mode 容错回落 revise。"""
        try:
            count = int(body.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if not 1 <= count <= 200:
            raise ValidationError("生成篇数须在 1–200 之间")
        contact_mode = str(body.get("contact_mode") or "revise")
        if contact_mode not in _CONTACT_MODES:
            contact_mode = "revise"
        return managed.begin_run(count, contact_mode)

    def pause(self) -> dict:
        return managed.pause_run()

    def status(self, lines: int = 200) -> dict:
        return managed.gen_snapshot(managed.current_tid(), lines=lines)
