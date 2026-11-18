"""generation 蓝图 · AI 短视频产线业务逻辑（video.html 契约）。

接口形状实测自 参考系统页面实测：
- GET  /api/video/list    → {packs:[…], gen_running, logs:[…]}（页面 4s 轮询）
- POST /api/video/generate → {ok} 或 {error}（入队即返回，进度走 list 轮询）
- POST /api/video/publish {id} → {ok, stage} 或 {error}
- POST /api/video/delete  {id} → {ok}
- GET  /api/video/sched   → {enabled, interval_min}
- POST /api/video/sched   {enabled, interval_min} → {ok}
- GET  /api/video/file/<id>.mp4 → 成片字节（封面帧由前端 #t=0.5 解出）

pack 字段：id/title/desc/tags/shots/city/business/duration/source/time/
published/published_at（页面读 title/desc/duration/city/time/published）。
"""
from __future__ import annotations

from app.errors import NotFound
from app.logging import get_logger
from app.platform import jobs
from app.services import video_store
from app.tenancy import current_tenant
from app.timeutil import now_local

log = get_logger(__name__)


class VideoService:
    # ── 列表（轮询面） ──────────────────────────────────────────────────────

    def list_videos(self) -> dict:
        tid = current_tenant()
        from app.blueprints.generation import video_jobs
        snap = video_jobs.snapshot(tid)
        return {"packs": video_store.list_videos(),
                "gen_running": snap["gen_running"], "logs": snap["logs"]}

    # ── 生成 ────────────────────────────────────────────────────────────────

    def start_generate(self) -> tuple[dict, int]:
        tid = current_tenant()
        from app.blueprints.generation import video_jobs
        if video_jobs.snapshot(tid)["gen_running"]:
            return ({"ok": False, "error": "已有视频生成任务在进行"}, 409)
        video_jobs.gen_begin(tid, 1)
        jobs.enqueue("video.generate", {}, tenant_id=tid, queue="gen")
        return ({"ok": True}, 200)

    # ── 发布 ────────────────────────────────────────────────────────────────

    def publish(self, video_id: str) -> tuple[dict, int]:
        try:
            video_store.read_meta(str(video_id or ""))
        except NotFound:
            return ({"ok": False, "error": f"视频 #{video_id} 不存在"}, 404)
        tid = current_tenant()
        jobs.enqueue("video.publish", {"id": str(video_id)},
                     tenant_id=tid, queue="gen")
        return ({"ok": True, "stage": "已入队，进度见生成台/视频库"}, 200)

    # ── 删除 ────────────────────────────────────────────────────────────────

    def delete(self, video_id: str) -> dict:
        try:
            video_store.delete_video(str(video_id or ""))
        except NotFound as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    # ── 自动发布计划 ────────────────────────────────────────────────────────

    def autopub_get(self) -> dict:
        cfg = video_store.load_autopub()
        try:
            interval = max(10, min(int(cfg.get("interval_min") or 120), 1440))
        except (TypeError, ValueError):
            interval = 120
        return {"enabled": bool(cfg.get("enabled")),
                "interval_min": interval}

    def autopub_save(self, body: dict) -> dict:
        try:
            interval = max(10, min(int(body.get("interval_min") or 120), 1440))
        except (TypeError, ValueError):
            interval = 120
        cfg = video_store.load_autopub()
        was_enabled = bool(cfg.get("enabled"))
        cfg.update(enabled=bool(body.get("enabled")), interval_min=interval)
        if cfg["enabled"] and not was_enabled:
            # 首次启用：last_run 置现在，保证"保存≠立即跑"，满一个间隔才发
            cfg["last_run"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
        video_store.save_autopub(cfg)
        return {"ok": True}
