"""抖音图文产线：生成/发布队列任务 + 进程内状态桥（douyin.html 轮询面）。

- 生成：每条 = LLM 出文案（title/description/tags，解析失败回落模板）+
  imagegen 出 N 张图（未配生图 = 内置演示占位图）→ 落 douyin/packs/<id>/。
- 发布：内容包与账号**一一配对**（轮流分配，不再 pack × 账号叉乘），逐条扣账号
  当日额度并跳过已打满的号；**云账号走蚁小二 YxerDriver**（与图文域外发布同一出口），
  暂停=不再起新任务；每笔落 ledger.json（页面按账本判定"已发布/失败可重发"）。
  计划的规则集中在 services/douyin_publish.py（页面显示的条数与实际条数同源）。
- 状态桥与 services/writer/managed.py 同一约定：模块 dict + 非重入 Lock，
  先取引用再进锁；单进程有效，重启清零（内容包与账本在磁盘，不丢）。
- 内容包组装（生图 + 文案 + 单张降级 + 失败回滚）在 services/douyin_pack.py。
"""
from __future__ import annotations

import threading
import traceback
from types import SimpleNamespace

from app.logging import get_logger
from app.platform.jobs import task
from app.platform.state_bridge import bridge
from app.models import Account
from app.services import douyin_store
from app.services.douyin_pack import build_pack
from app.tenancy import TenantScopedRepository, current_tenant

log = get_logger(__name__)

_LOCK = threading.Lock()
_LOG_CAP = 200
_EMPTY = {"gen_running": False, "gen_stop": False, "gen_cur": 0, "gen_total": 0,
          "gen_pending": 0, "gen_logs": [], "pub_active": False, "pub_paused": False,
          "pub_done": 0, "pub_total": 0, "pub_waiting": 0, "pub_cur": "", "pub_logs": [],
          "poipool_running": False}
# tid → 状态 dict（有界容器：超上限驱逐最久未访问的租户，避免只增不减）
_DY = bridge(lambda: {**_EMPTY, "gen_logs": [], "pub_logs": []})


def _st(tid: str) -> dict:
    return _DY.get(tid)


def gen_begin(tid: str, count: int) -> None:
    st = _st(tid)
    with _LOCK:
        st.update(gen_running=True, gen_stop=False, gen_cur=0, gen_total=count,
                  gen_pending=count, gen_made=[])
        st["gen_logs"] = []


def gen_request_stop(tid: str) -> None:
    _st(tid)["gen_stop"] = True


def pub_begin(tid: str, total: int) -> None:
    st = _st(tid)
    with _LOCK:
        st.update(pub_active=True, pub_paused=False, pub_done=0, pub_total=total,
                  pub_waiting=total, pub_cur="")
        st["pub_logs"] = []


def pub_set_paused(tid: str, paused: bool) -> bool:
    st = _st(tid)
    with _LOCK:
        active = bool(st["pub_active"])
        if active:
            st["pub_paused"] = paused
    return active


def snapshot(tid: str) -> dict:
    st = _st(tid)
    with _LOCK:
        return {
            "gen": {"running": st["gen_running"], "cur": st["gen_cur"],
                    "total": st["gen_total"], "pending": st["gen_pending"]},
            "gen_logs": list(st["gen_logs"][-60:]),
            "gen_made": list(st.get("gen_made") or []),
            "pub": {"active": st["pub_active"], "paused": st["pub_paused"],
                    "done": st["pub_done"], "total": st["pub_total"],
                    "waiting": st["pub_waiting"], "cur_account": st["pub_cur"]},
            "pub_logs": list(st["pub_logs"][-60:]),
            "poipool": {"running": bool(st.get("poipool_running"))},
        }


def reset_states() -> None:
    with _LOCK:
        _DY.clear()


def _log(st: dict, kind: str, line: str) -> None:
    with _LOCK:
        st[kind].append(line)
        del st[kind][:-_LOG_CAP]


# ── 生成 ────────────────────────────────────────────────────────────────────

@task("douyin.generate.batch")
def dy_generate_job(payload: dict) -> dict:
    from app.blueprints.generation.douyin_service import DouyinService  # 延迟：避免循环导入

    tid = current_tenant()
    st = _st(tid)
    cfg = DouyinService().get_config()
    count = max(1, min(int(payload.get("count") or 1), 30))   # 21 条/周要一次排完
    made = 0
    try:
        for i in range(count):
            if st["gen_stop"]:
                _log(st, "gen_logs", "⏸ 收到停止指令，剩余排队取消")
                break
            # 库存上限：自动补货不经 HTTP 端点（start_generate 那道闸拦不到），
            # 无人值守的定时发布 + 大 `gen_batch` 会把磁盘堆满（实测 46 个包 469MB）。
            stock = len(douyin_store.list_packs())
            if stock >= int(cfg.get("max_stock") or 80):
                _log(st, "gen_logs",
                     f"⏹ 内容包库存已满（{stock} 个，上限 {cfg.get('max_stock')}）："
                     "已停止本批生成，请先发布或删除旧内容包")
                break
            with _LOCK:
                st["gen_cur"] = i + 1
                st["gen_pending"] = count - i - 1
            try:
                pack = build_pack(cfg)
                made += 1
                # 记下本批真实产出的包：状态桥的"本次已出 N 包"以前取的是
                # `list_packs()[-30:]`（最近 30 个），与"本次"无关 —— 生成 3 条
                # 也可能显示 30，用户据此误判产出量。
                with _LOCK:
                    st["gen_made"].append({"id": pack["id"], "title": pack["title"]})
                    del st["gen_made"][:-20]
                warn = "（含占位图）" if _degraded(pack) else ""
                _log(st, "gen_logs",
                     f"✅ #{pack['id']}《{pack['title'][:22]}》配图 {len(pack['images'])} 张{warn}")
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断队列
                # 失败必须落**磁盘**日志：状态桥在内存里，服务一重启失败原因就永久
                # 丢失 —— 用户只会看到"少生成了几条"，再也没法归因（本次事故即如此）。
                log.warning("douyin_pack_failed", extra={
                    "event": "douyin_pack_failed", "index": i + 1,
                    "err": str(exc)[:200], "trace": traceback.format_exc()[-600:]})
                _log(st, "gen_logs", f"❌ 第 {i + 1} 条生成失败：{str(exc)[:60]}")
    finally:
        with _LOCK:
            st["gen_running"] = False
    return {"ok": True, "made": made}


def _degraded(pack: dict) -> bool:
    """内容包里是否带"降级为内置占位图"的质检标记（供进度日志如实提示）。"""
    return any("占位图" in str(q) for q in (pack.get("qc") or []))


# ── 发布 ────────────────────────────────────────────────────────────────────

@task("douyin.publish.batch")
def dy_publish_job(payload: dict) -> dict:
    tid = current_tenant()
    st = _st(tid)
    packs = [str(x) for x in (payload.get("pack_ids") or [])]
    accounts = [str(x) for x in (payload.get("account_ids") or [])]
    from app.blueprints.generation.douyin_service import DouyinService  # 延迟：避免循环导入
    from app.services import douyin_publish

    # 计划在**任务里**定，口径来自入队时页面选的那个 `mode`（each=每个勾选的号
    # 各发一份 / rotate=一包只发一个号），跳过已发过的（each 按「包×号」为单位）、
    # 停用/额度已满的号。页面上的条数、日志行、账本条目从此同源。
    mode = douyin_publish.resolve_mode(payload.get("mode"))
    pairs, notes = douyin_publish.plan(packs, accounts, DouyinService().accounts(),
                                       mode=mode)
    total = len(pairs)
    with _LOCK:
        st["pub_total"] = total        # 真实总数回写状态桥（入队时只能按勾选数估）
        st["pub_waiting"] = total
    for note in notes:
        _log(st, "pub_logs", f"⏭ {note}")

    # 定位分配（2026-10-19）：整批一次性分配，保证账号间 POI 不重复、同账号换区、
    # 同地区换地址；池为空时返回全 None（发布照常，只是不加定位）。
    from app.services import douyin_location
    locs = douyin_location.allocate_batch(pairs, overrides=douyin_location.overrides_from_payload(
        payload.get("locations")))
    from app.blueprints.generation import douyin_poi_jobs
    douyin_poi_jobs.ensure_pool_built(pairs, locs, st)

    done = 0
    # 本批次已失败的账号不再重试：账号级错误（未设置代理 / 额度 / 登录失效）
    # 换个内容包也还是同一个错，重试只是白传一批图 + 等最长 300s 超时
    # （实测：一个号连撞 7 次"未设置代理"）。
    failed_accounts: set[str] = set()
    exhausted: set[str] = set()        # 当日额度已满的号（次日 0 点重置）
    try:
        for pid, aid in pairs:
            if st["pub_paused"]:
                _log(st, "pub_logs", "⏸ 已暂停：剩余任务不再起跑（恢复后可重发）")
                with _LOCK:
                    st["pub_waiting"] = max(0, total - done)
                return {"ok": True, "done": done, "paused": True}
            if aid in failed_accounts or aid in exhausted:
                done += 1
                _log(st, "pub_logs", f"⏭ #{pid} → {_account_name(aid)} 跳过（本批次该账号已跳过）")
                with _LOCK:
                    _progress(st, done, total)
                continue
            with _LOCK:
                st["pub_cur"] = aid
            # 当日额度：发出前**原子**占用一次（与文章发布同一张表、同一条 SQL）。
            # 改造前抖音这条线从不读额度 —— 卡片上"剩余 N 条"永远显示满额，
            # 号打满了照发，再被平台打回一次。额度按"尝试"扣（与文章发布一致）。
            if douyin_publish.quota_claim(aid) == "quota":
                exhausted.add(aid)
                done += 1
                _log(st, "pub_logs",
                     f"⏸ 账号 {_account_name(aid)} 当日额度已满，本次跳过（次日 0 点恢复）")
            else:
                try:
                    url = _publish_one(pid, aid, location=locs.get((pid, aid)))
                    done += 1
                    _log(st, "pub_logs",
                         f"✅ #{pid} → {_account_name(aid)} 发布成功 {url[:48]}")
                except Exception as exc:  # noqa: BLE001
                    # 失败也记原因：账本只有 ok=false 时，用户/排查都看不出为啥失败
                    failed_accounts.add(aid)
                    douyin_store.ledger_append(pid, {
                        "ok": False, "account": _account_name(aid),
                        "account_id": aid, "time": douyin_store.now_stamp(),
                        "error": str(exc)[:300]})
                    _log(st, "pub_logs",
                         f"❌ #{pid} → {_account_name(aid)} 失败：{str(exc)[:60]}")
            with _LOCK:
                _progress(st, done, total)
    finally:
        with _LOCK:
            st["pub_active"] = False
            st["pub_cur"] = ""
        _sync_timed_accounts(packs)
    return {"ok": True, "done": done, "skipped": len(notes)}


def _sync_timed_accounts(packs: list) -> None:
    """把排期表上的 `accounts` 校正成**实际发到几个号**。

    `douyin_timed.fire()` 派单时记的是**在线账号数**，与 plan 实际派出的对数、
    账本里的成功记录都不是一回事 —— 已发过的号 / 当日额度不足的号会被跳过，
    失败另有账目。不校正的话排期表会一直显示"发到 11 个号"而实际只发了 1 个
    （2026-10-21 实测：#68 账面 11、实发 1）。只动 state=fired 的条目，
    手动发布的包没有排期记录，不受影响。
    """
    try:
        led = douyin_store.load_ledger()
        with douyin_store.timed_lock():
            book = douyin_store.load_timed()
            dirty = False
            for pid in packs:
                ent = book.get(str(pid))
                if not (isinstance(ent, dict) and ent.get("state") == "fired"):
                    continue
                n = len({str(r.get("account_id")) for r in (led.get(str(pid)) or [])
                         if r.get("ok")})
                if int(ent.get("accounts") or 0) != n:
                    ent["accounts"] = n
                    dirty = True
            if dirty:
                douyin_store.save_timed(book)
    except Exception as exc:  # noqa: BLE001 - 记账校正失败不影响发布结果
        log.warning("douyin_timed_accounts_sync_failed", extra={
            "event": "douyin_timed_accounts_sync_failed", "err": str(exc)[:160]})


def _progress(st: dict, done: int, total: int) -> None:
    st["pub_done"] = done
    st["pub_waiting"] = max(0, total - done)


def _account_name(account_id: str) -> str:
    """把 yx-xxxx 账号 ID 解析成**可读的抖音号名称**。

    以前账本直接记 account_id（形如 yx-6aadf81ed033d36c0b7dfcd8），页面 badge
    上是一串乱码，用户根本看不出这条图文发到了哪个抖音号（2026-10-19 反馈）。
    查不到就退回 ID —— 不编造名字。
    """
    if not account_id.startswith("yx-"):
        return account_id          # 演示模式等内置假账号
    row = (TenantScopedRepository(Account).query()
           .filter(Account.id == account_id).first())
    return str(getattr(row, "name", "") or "").strip() or account_id


def _publish_one(pack_id: str, account_id: str, location: dict | None = None) -> str:
    # 抖音图文走 imageText 驱动（多图 + 文案）。修复前误用文章型 YxerDriver：
    # 其平台表不含 douyin_imgtext，未调 CLI 即抛"平台不在文章发布支持范围"，
    # 抖音图文发布 100% 失败。
    from app.services.publish.yxer_imgtext import YxerImgtextDriver

    pack = douyin_store.read_pack(pack_id)
    files = [str(f) for f in (pack.get("images") or [])]
    paths = [douyin_store.image_path(pack_id, f) for f in files]
    tags = [str(t) for t in (pack.get("tags") or [])]
    desc = (pack.get("description") or "").strip()
    if tags:                              # 图文描述惯例带话题
        desc = (desc + "\n" + " ".join(f"#{t}" for t in tags)).strip()
    acct = SimpleNamespace(id=account_id, platform="douyin_imgtext",
                           yxer_id=account_id[3:] if account_id.startswith("yx-") else account_id,
                           name=_account_name(account_id))
    url = YxerImgtextDriver().publish(
        images=paths, title=pack.get("title") or f"图文#{pack_id}",
        desc=desc, tags=tags, account=acct, city=pack.get("city"),
        location=location)
    # 账本记**可读的账号名**（页面 badge 显示"发到哪个号"），另存 account_id
    # 供轮换排序与排查；以前只记 ID，看起来就是一堆乱码。
    douyin_store.ledger_append(pack_id, {
        "ok": True, "account": _account_name(account_id), "account_id": account_id,
        "time": douyin_store.now_stamp(), "url": url})
    return url


# ── 定位池构建（重活：必须后台跑，不能进发布主链路）─────────────────────────

def poipool_begin(tid: str) -> None:
    with _LOCK:
        _st(tid)["poipool_running"] = True


def poipool_end(tid: str) -> None:
    with _LOCK:
        _st(tid)["poipool_running"] = False


# 任务本体在 douyin_poi_jobs.py（建池是重活，拆出去控制本文件 300 行门禁）。
