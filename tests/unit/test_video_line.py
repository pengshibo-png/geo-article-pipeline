"""AI 短视频产线：存储 / 生成 / 发布 / 文件服务 / 自动发布 测试。

生成走真实 ffmpeg（随包 imageio-ffmpeg）；文案用 fake LLM → 模板兜底，
全链路可离线跑通。账号发布走 mock（无真实云账号），如实断言不真发出去。
"""
from __future__ import annotations

import io
import json

from app.extensions import db
from app.tenancy import tenant_scope
from tests.conftest import T1, login


# ── 存储层 ──────────────────────────────────────────────────────────────────

def test_video_store_roundtrip(app):
    from app.services import video_store

    with app.app_context(), tenant_scope(T1):
        assert video_store.next_video_id() == 1
        meta = {"id": 1, "title": "演示片", "published": False,
                "time": "2026-10-14 12:00:00"}
        video_store.write_meta(meta)
        video_store.save_video(1, b"\x00\x00\x00\x18ftypmp42")
        video_store.save_cover(1, b"cover-png")
        assert video_store.next_video_id() == 2
        got = video_store.read_meta(1)
        assert got["title"] == "演示片"
        assert video_store.video_path(1).exists()
        assert video_store.list_videos()[0]["id"] == 1
        video_store.delete_video(1)
        assert video_store.list_videos() == []


def test_video_autopub_and_ledger(app):
    from app.services import video_store

    with app.app_context(), tenant_scope(T1):
        video_store.save_autopub({"enabled": True, "interval_min": 30})
        cfg = video_store.load_autopub()
        assert cfg["enabled"] and cfg["interval_min"] == 30
        video_store.ledger_append(1, {"ok": True, "account": "yx-a", "time": "t"})
        assert video_store.last_account_publish_at("yx-a") == "t"
        # 反查：账号轮换按"最久没发的优先"
        video_store.ledger_append(2, {"ok": True, "account": "yx-b", "time": "z"})
        video_store.ledger_append(3, {"ok": True, "account": "yx-a", "time": "y"})
        assert video_store.last_account_publish_at("yx-a") == "y"


# ── 生成 provider（真实 ffmpeg）──────────────────────────────────────────────

def test_render_mock_produces_mp4():
    from app.services.video_provider import render_mock

    mp4 = render_mock(title="测试视频", shots=["分镜一", "分镜二"])
    assert isinstance(mp4, bytes) and len(mp4) > 256
    assert b"ftyp" in mp4[:64]                 # MP4 box 魔数


def test_render_cover_fallback_without_ffmpeg(monkeypatch):
    """ffmpeg 缺失时封面退化为纯色 PNG（仍可被前端用）。"""
    from app.services import video_provider

    monkeypatch.setattr(video_provider, "ffmpeg_exe", lambda: None)
    png = video_provider.render_cover(title="X")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# ── 服务 / API 层 ─────────────────────────────────────────────────────────────

def test_video_list_empty(client):
    login(client, "alpha", "pw-alpha")
    d = client.get("/api/video/list").get_json()
    assert d["packs"] == [] and d["gen_running"] is False and "logs" in d


def test_video_sched_get_set(client):
    login(client, "alpha", "pw-alpha")
    assert client.get("/api/video/sched").get_json()["enabled"] is False
    r = client.post("/api/video/sched", json={"enabled": True, "interval_min": 45})
    assert r.get_json() == {"ok": True}
    cfg = client.get("/api/video/sched").get_json()
    assert cfg["enabled"] is True and cfg["interval_min"] == 45


def test_video_file_endpoint(client, app):
    login(client, "alpha", "pw-alpha")
    # 不存在 → 404；非法编号 → 404
    assert client.get("/api/video/file/999.mp4").status_code == 404
    assert client.get("/api/video/file/x.mp4").status_code == 404
    with app.app_context(), tenant_scope(T1):
        from app.services import video_store
        video_store.save_video(7, b"\x00\x00\x00\x18ftypmp42")
    r = client.get("/api/video/file/7.mp4")
    assert r.status_code == 200 and r.mimetype == "video/mp4"


def test_video_generate_full_loop(client, app):
    """整链路：生成（fake LLM→模板兜底 + 真实 ffmpeg 合成）→ 列表可见 → 文件可服务。"""
    login(client, "alpha", "pw-alpha")
    r = client.post("/api/video/generate", json={})
    assert r.get_json() == {"ok": True}
    # 轮询直到生成完成
    snap = _poll_video(client, timeout=60)
    packs = snap["packs"]
    assert len(packs) == 1
    vid = packs[0]["id"]
    assert packs[0]["source"] == "mock"          # 离线演示，如实标注
    assert packs[0]["title"]                     # 模板兜底也给了标题
    # 文件真实存在且是 mp4
    resp = client.get(f"/api/video/file/{vid}.mp4")
    assert resp.status_code == 200 and b"ftyp" in resp.data[:64]


def test_video_publish_needs_real_account(client, app):
    """无真实云账号 → 发布如实失败（mock 模式未开，绝不假装发出去）。"""
    login(client, "alpha", "pw-alpha")
    with app.app_context(), tenant_scope(T1):
        from app.services import video_store
        video_store.write_meta({"id": 3, "title": "待发", "published": False})
    r = client.post("/api/video/publish", json={"id": "3"})
    assert r.get_json().get("ok") is True       # 入队成功
    # 真实执行会缺账号 → 任务失败，账本记 ok=False（轮询账本，确定性）
    import time
    end = time.monotonic() + 40
    failed = False
    while time.monotonic() < end:
        with app.app_context(), tenant_scope(T1):
            rows = video_store.load_ledger().get("3", [])
        if rows and rows[-1].get("ok") is False:
            failed = True
            break
        time.sleep(0.5)
    assert failed, "发布任务应在缺账号时失败并写入账本"


# ── 调度器：视频自动发布真正执行 ────────────────────────────────────────────

def test_scheduler_fires_video_autopub(app, monkeypatch):
    from app.services import video_store
    from app.services import scheduler

    calls = []
    monkeypatch.setattr("app.platform.jobs.enqueue",
                       lambda name, payload, **kw: calls.append((name, payload)))

    with app.app_context(), tenant_scope(T1):
        video_store.save_autopub(
            {"enabled": True, "interval_min": 120,
             "last_run": "2026-10-13 00:00:00"})   # 已过期 → 应触发
        # 库存空 → 自动补产
        from datetime import datetime
        res = scheduler.tick(now=datetime(2026, 10, 14, 12, 0, 0))
    assert ("t_alpha:video:generate",) in [(c[0],) for c in calls] \
        or any(c[0] == "video.generate" for c in calls)
    assert "t_alpha:video:generate" in res["fired"]


def test_scheduler_video_autopub_skips_when_not_due(app, monkeypatch):
    from app.services import video_store
    from app.services import scheduler

    calls = []
    monkeypatch.setattr("app.platform.jobs.enqueue",
                       lambda name, payload, **kw: calls.append((name, payload)))

    with app.app_context(), tenant_scope(T1):
        video_store.save_autopub(
            {"enabled": True, "interval_min": 120,
             "last_run": "2026-10-14 11:59:00"})   # 未到期 → 不触发
        from datetime import datetime
        res = scheduler.tick(now=datetime(2026, 10, 14, 12, 0, 0))
    assert not any(c[0] == "video.generate" for c in calls)
    assert "t_alpha:video:generate" not in res["fired"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _poll_video(client, timeout: float = 60) -> dict:
    import time

    end = time.monotonic() + timeout
    last = {}
    while time.monotonic() < end:
        last = client.get("/api/video/list").get_json()
        if not last["gen_running"]:
            return last
        time.sleep(0.5)
    return last
