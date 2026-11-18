"""抖音图文产线的文件存储：配置 / 内容包 / 发布账本（data/<tenant>/douyin/）。

与文章三件套同构（ADR-006：元数据+正文落盘，租户目录防穿越）：
    douyin/config.json         产线配置（提示词/张数/城市/违禁词/生图渠道/计划）
    douyin/autopub.json        循环发布配置
    douyin/packs/<id>/pack.json 内容包（标题/文案/标签/图片文件名/城市/质检）
    douyin/packs/<id>/images/*  生成的图片文件
    douyin/ledger.json          发布账本 {pack_id: [{ok,account,time}]}（append-only）

图片 URL 由 /media/douyin/<pack_id>/<file> 服务（generation 蓝图路由）。
"""
from __future__ import annotations

import contextlib
import json
import re
import threading
from pathlib import Path

from flask import current_app

from app.errors import NotFound
from app.platform.fsutil import remove_tree
from app.tenancy import current_tenant
from app.timeutil import now_local

# 账本是"读-改-写"全量回写的：并发发布会把彼此的账目覆盖掉，故加锁。
_LEDGER_LOCK = threading.Lock()

_PACK_ID_RE = re.compile(r"^\d{1,12}$")
_NAME_RE = re.compile(r"^[\w.\-]{1,80}$")


def root() -> Path:
    return Path(current_app.config["DATA_ROOT"]) / current_tenant() / "douyin"


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


# ── 配置 / 循环发布 ─────────────────────────────────────────────────────────

def load_config(default: dict | None = None) -> dict:
    return _read_json(root() / "config.json", default if default is not None else {})


def save_config(data: dict) -> None:
    _write_json(root() / "config.json", data)


# 循环发布配置同样是"读-改-写"全量回写。**非重入锁**：持锁期间不得再调
# `autopub_lock` / `update_autopub`（同线程会直接死锁）—— 要"保存并立即发一篇"时
# 必须把派发动作放在锁**外**（见 douyin_service.autopub_save）。
_AUTOPUB_LOCK = threading.Lock()


def load_autopub() -> dict:
    return _read_json(root() / "autopub.json", {})


def save_autopub(data: dict) -> None:
    _write_json(root() / "autopub.json", data)


@contextlib.contextmanager
def autopub_lock():
    """自动发布配置的读-改-写互斥。

    调度线程（每 60 秒 tick）与 HTTP 请求（保存配置 / 「保存并立即发一篇」）会
    同时改这一份 JSON：原来 load → 改 → save 三步无锁，谁后写谁赢 —— 调度刚记下
    的 `last_run` / `last_shot` 被用户的一次保存覆盖，于是"刚发过"立刻又发一次
    （重复发稿）。两边都必须在这个锁里做整段读改写。
    """
    with _AUTOPUB_LOCK:
        yield


def update_autopub(mutate) -> dict:
    """锁内读-改-写 autopub.json；`mutate(cfg)` 直接改传入的 dict，返回落盘后的配置。"""
    with _AUTOPUB_LOCK:
        cfg = load_autopub()
        mutate(cfg)
        save_autopub(cfg)
        return cfg


# ── 每条作品的定时发布（pack_id → 一次性发布时刻）──────────────────────────
# 单独一份 timed.json，**不与 pack.json 混写**：pack.json 由「编辑内容包」整份
# 读-改-回写，若把 `fired` 标记也塞进 pack.json，用户恰好在调度点编辑标题，就会
# 把标记覆盖回 pending → 同一条图文被发第二遍。两个文件各只有一个写者，从结构上
# 没有这个竞争。
_TIMED_LOCK = threading.Lock()


def load_timed() -> dict:
    return _read_json(root() / "timed.json", {})


def save_timed(data: dict) -> None:
    _write_json(root() / "timed.json", data)


@contextlib.contextmanager
def timed_lock():
    """定时发布清单的读-改-写互斥（调度线程派单 vs HTTP 保存定时）。"""
    with _TIMED_LOCK:
        yield


# ── 定位池 / 定位分配状态 ────────────────────────────────────────────────────
# poi_pool.json 地区→POI 列表（建池写/发布读）；loc_state.json 账号上次地区 + 地区本轮已用地址。
# 两者都是整份 JSON 全量回写：建池任务与发布分配可能并发，必须持锁读-改-写。
_POOL_LOCK = threading.Lock()
_LOC_STATE_LOCK = threading.Lock()


def load_poi_pool() -> dict:
    return _read_json(root() / "poi_pool.json", {})


def save_poi_pool(data: dict) -> None:
    _write_json(root() / "poi_pool.json", data)


@contextlib.contextmanager
def poi_pool_lock():
    """定位池的读-改-写互斥（建池任务 vs 发布侧读取）。"""
    with _POOL_LOCK:
        yield


def update_poi_pool(mutate) -> dict:
    """锁内读-改-写 poi_pool.json；`mutate(pool)` 直接改传入 dict，返回落盘后的池。"""
    with _POOL_LOCK:
        pool = load_poi_pool()
        mutate(pool)
        save_poi_pool(pool)
        return pool


def load_loc_state() -> dict:
    return _read_json(root() / "loc_state.json", {})


def save_loc_state(data: dict) -> None:
    _write_json(root() / "loc_state.json", data)


@contextlib.contextmanager
def loc_state_lock():
    """定位分配状态的读-改-写互斥（同进程内并发发布批次）。"""
    with _LOC_STATE_LOCK:
        yield


def update_loc_state(mutate) -> dict:
    """锁内读-改-写 loc_state.json；`mutate(state)` 直接改传入 dict，返回落盘后的状态。"""
    with _LOC_STATE_LOCK:
        state = load_loc_state()
        mutate(state)
        save_loc_state(state)
        return state


# ── 内容包 ──────────────────────────────────────────────────────────────────

def packs_dir() -> Path:
    return root() / "packs"


_ID_LOCK = threading.Lock()      # next_pack_id 的并发保护（同进程多 worker）


def next_pack_id() -> int:
    """取下一个可用内容包编号。

    **必须加锁 + 跳过已存在目录**：旧实现是 `max(目录名)+1` 且无锁，而生成队列是
    `platform/jobs.py` 里 4 个 worker 的作业池 —— 两个作业会拿到**同一个 pid**，
    `img1..n` 与 `pack.json` 互相覆盖（一个包的图配另一个包的文案）；若其中一个
    组装失败，`discard_pack(pid)` 还会把**已成功的同号包整目录删掉**（丢包 + 跳号）。
    """
    with _ID_LOCK:
        existing = {int(p.name) for p in packs_dir().glob("*")
                    if p.is_dir() and _PACK_ID_RE.match(p.name)}
        nid = (max(existing) + 1) if existing else 1
        while nid in existing:      # 防御：目录被外部创建/并发残留时继续往后找
            nid += 1
        # **在锁内就把目录占住**：光"扫目录取最大+1"挡不住并发 —— 目录要到出图
        # （`save_image`）才真正创建，而文案+图纲要跑几十秒；这段窗口里两个调用方
        # 会拿到**同一个 pid**，随后互相覆盖（一个包的图配另一个包的文案）。
        # 占位目录没有 pack.json，`list_packs` 看不见它，正常失败路径由 discard_pack 清掉。
        pack_dir(nid).mkdir(parents=True, exist_ok=True)
        return nid


def pack_dir(pack_id) -> Path:
    sid = str(pack_id)
    if not _PACK_ID_RE.match(sid):
        raise NotFound("非法内容包编号")
    return packs_dir() / sid


def read_pack(pack_id) -> dict:
    p = pack_dir(pack_id) / "pack.json"
    if not p.exists():
        raise NotFound(f"内容包 #{pack_id} 不存在")
    return json.loads(p.read_text(encoding="utf-8"))


def write_pack(pack: dict) -> dict:
    _write_json(pack_dir(pack["id"]) / "pack.json", pack)
    return pack


def save_image(pack_id, filename: str, data: bytes) -> None:
    if not _NAME_RE.match(filename):
        raise NotFound("非法图片名")
    p = pack_dir(pack_id) / "images" / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def read_image(pack_id, filename: str) -> bytes:
    if not _NAME_RE.match(filename):
        raise NotFound("非法图片名")
    p = pack_dir(pack_id) / "images" / filename
    if not p.exists():
        raise NotFound("图片不存在")
    return p.read_bytes()


def image_path(pack_id, filename: str) -> Path:
    return pack_dir(pack_id) / "images" / filename


def list_packs() -> list:
    out = []
    d = packs_dir()
    if not d.exists():
        return out
    for sub in sorted(d.iterdir(), key=lambda x: int(x.name) if x.name.isdigit() else 0):
        pj = sub / "pack.json"
        if pj.exists():
            try:
                out.append(json.loads(pj.read_text(encoding="utf-8")))
            except ValueError:
                continue
    return out


def delete_pack(pack_id) -> None:
    d = pack_dir(pack_id)
    if not d.exists():
        raise NotFound(f"内容包 #{pack_id} 不存在")
    remove_tree(d)


def discard_pack(pack_id) -> None:
    """回滚一个**没组装成功**的内容包目录（只在组装异常时调用）。

    与 `delete_pack` 的差别：目录不存在时**静默返回**。回滚路径上"还没建目录"
    是正常情况，不该再抛 NotFound 把真实错误盖掉。

    为什么需要它：生图是"边出图边落盘、最后才写 pack.json"，中途失败会留下
    只有 images/ 没有 pack.json 的孤儿目录 —— 页面看不到、没人清理，还因为
    `next_pack_id()` 取目录名最大值+1 而**吃掉编号**（用户会看到跳号的内容包）。
    """
    d = pack_dir(pack_id)
    if d.exists():
        remove_tree(d)


# ── 发布账本 ────────────────────────────────────────────────────────────────

def load_ledger() -> dict:
    return _read_json(root() / "ledger.json", {})


def ledger_append(pack_id, record: dict) -> None:
    """追加一条账目。账本是整份 JSON 全量回写，**必须在锁内**读-改-写，
    否则并发发布会互相覆盖（丢账目）。"""
    with _LEDGER_LOCK:
        led = load_ledger()
        led.setdefault(str(pack_id), []).append(record)
        _write_json(root() / "ledger.json", led)


def now_stamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")
