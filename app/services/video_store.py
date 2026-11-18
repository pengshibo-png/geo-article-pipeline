"""AI 短视频产线的文件存储：成片 / 封面 / 自动发布配置 / 发布账本。

与抖音图文产线（douyin_store）同构（ADR-006：元数据+正文落盘，租户目录防穿越）：
    videos/autopub.json       自动发布配置 {enabled, interval_min, last_run}
    videos/<id>/meta.json     视频元数据（标题/文案/标签/分镜/城市/时长/发布态）
    videos/<id>/video.mp4     成片（竖版 9:16）
    videos/<id>/cover.png     封面帧
    videos/ledger.json        发布账本 {video_id: [{ok,account,time,url}]}（append-only）

文件服务由 generation 蓝图 `/api/video/file/<id>.mp4` 提供。
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from flask import current_app

from app.errors import NotFound
from app.platform.fsutil import remove_tree
from app.tenancy import current_tenant
from app.timeutil import now_local

_VIDEO_ID_RE = re.compile(r"^\d{1,12}$")


def root() -> Path:
    return Path(current_app.config["DATA_ROOT"]) / current_tenant() / "videos"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ── 自动发布配置 ────────────────────────────────────────────────────────────

def load_autopub() -> dict:
    return _read_json(root() / "autopub.json", {})


def save_autopub(data: dict) -> None:
    _write_json(root() / "autopub.json", data)


# ── 视频 ───────────────────────────────────────────────────────────────────

def videos_dir() -> Path:
    return root()


def next_video_id() -> int:
    existing = [int(p.name) for p in videos_dir().glob("*")
                if p.is_dir() and _VIDEO_ID_RE.match(p.name)]
    return (max(existing) + 1) if existing else 1


def video_dir(video_id) -> Path:
    sid = str(video_id)
    if not _VIDEO_ID_RE.match(sid):
        raise NotFound("非法视频编号")
    return videos_dir() / sid


def read_meta(video_id) -> dict:
    p = video_dir(video_id) / "meta.json"
    if not p.exists():
        raise NotFound(f"视频 #{video_id} 不存在")
    return json.loads(p.read_text(encoding="utf-8"))


def write_meta(meta: dict) -> dict:
    _write_json(video_dir(meta["id"]) / "meta.json", meta)
    return meta


def save_video(video_id, data: bytes) -> None:
    p = video_dir(video_id) / "video.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def video_path(video_id) -> Path:
    return video_dir(video_id) / "video.mp4"


def save_cover(video_id, data: bytes) -> None:
    p = video_dir(video_id) / "cover.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def cover_path(video_id) -> Path:
    return video_dir(video_id) / "cover.png"


def list_videos() -> list:
    out = []
    d = videos_dir()
    if not d.exists():
        return out
    for sub in sorted(d.iterdir(), key=lambda x: int(x.name) if x.name.isdigit() else 0):
        mj = sub / "meta.json"
        if mj.exists():
            try:
                out.append(json.loads(mj.read_text(encoding="utf-8")))
            except ValueError:
                continue
    return out


def delete_video(video_id) -> None:
    d = video_dir(video_id)
    if not d.exists():
        raise NotFound(f"视频 #{video_id} 不存在")
    remove_tree(d)


# ── 发布账本 ────────────────────────────────────────────────────────────────

# 账本是"读-改-写"全量回写的：并发发布会把彼此的账目覆盖掉，故加锁。
_LEDGER_LOCK = threading.Lock()

def load_ledger() -> dict:
    return _read_json(root() / "ledger.json", {})


def ledger_append(video_id, record: dict) -> None:
    """追加一条账目。账本整份 JSON 全量回写，**必须在锁内**读-改-写，
    否则并发发布会互相覆盖（丢账目）。"""
    with _LEDGER_LOCK:
        led = load_ledger()
        led.setdefault(str(video_id), []).append(record)
        _write_json(root() / "ledger.json", led)


def last_publish_at(video_id) -> str:
    """该视频最近一次发布时间（无记录返回空串）。"""
    rows = load_ledger().get(str(video_id)) or []
    return rows[-1].get("time", "") if rows else ""


def last_account_publish_at(account_id: str) -> str:
    """该账号最近一次成功发布时间（账号轮换排序用；账本按视频记账，反查）。"""
    stamp = ""
    for rows in load_ledger().values():
        for r in rows:
            if r.get("ok") and r.get("account") == str(account_id):
                stamp = max(stamp, str(r.get("time") or ""))
    return stamp


def now_stamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")
