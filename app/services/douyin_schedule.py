"""抖音图文「自动发布」：配置归一 + 触发（从 douyin_service / scheduler 拆出）。

两个模式：
- `interval`：每 N 分钟发 1 篇（`interval_min`），到点补货（`gen_every_min`）
- `schedule`：每天在指定时刻各发 N 条（2026-10-18 新增）

拆出来的原因：这两块逻辑一边属于"配置校验"（douyin_service）、一边属于"调度触发"
（scheduler），放一起会让两个文件都撞 300 行门禁；而且它们本来就是同一件事的两半。
2026-10-19 复查又把 interval 模式整块搬了过来 —— 它原来留在 scheduler 里，于是：

- **`force` 在循环模式下不生效**：「保存并立即发一篇」点了没反应（force 只在
  schedule 分支被读），用户只能干等一个间隔；
- **`gen_every_min` / `gen_mode=interval` 是死配置**：代码只判断 `!= "off"`，
  页面存下的"每隔 N 分钟补货"从来没被读过；
- 库存挑选只看"有没有 ok 账目"，失败的包被每轮 tick 反复挑中（见 douyin_publish）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.logging import get_logger
from app.platform import jobs
from app.services.douyin_config import (   # noqa: F401  再导出：配置归一仍从这里取
    _MAX_SLOTS,
    build_autopub as build_config,
    clamp as _clamp,
    norm_schedule,
    norm_time,
)
from app.timeutil import shanghai

log = get_logger(__name__)

SCHEDULE_CATCHUP_MIN = 15      # 定时档允许迟到补发的分钟数（防 tick 漂移漏发）


def _aware(dt):
    """naive → aware。配置里落盘的是 naive 本地时间串，而 tick 传的是 aware ——
    不归一会 `TypeError: can't subtract offset-naive and offset-aware datetimes`，
    被 tick 的 except 吞掉后表现为"自动发布跑一次就永久失效"。"""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=shanghai())


def _ts(raw) -> datetime | None:
    try:
        return _aware(datetime.strptime(str(raw or ""), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M:%S")


def next_in_sec(cfg: dict, now: datetime) -> int | None:
    """距离下一次自动发布还有多少秒（页面上的倒计时）。

    以前后端返回的 autopub 配置里根本没有这个字段，页面无条件读
    `j.next_in_sec` → 倒计时恒为 `NaN:NaN`。这里按模式算真实值：
    interval = 间隔 - 已过时间（不小于 0）；schedule = 下一个启用档的时刻，
    尊重每档的 `days`（不到那天不算，最多往后找一周）。算不出来返回 None，
    页面据此显示"未启用"。
    """
    if not cfg.get("enabled"):
        return None
    now = _aware(now)
    if cfg.get("mode") != "schedule":
        last = _ts(cfg.get("last_run"))
        if last is None:
            return 0
        every = _clamp(cfg.get("interval_min"), 5, 720, 30) * 60
        return max(0, int(every - (now - last).total_seconds()))

    best: datetime | None = None
    for slot in (cfg.get("schedule") or []):
        if not isinstance(slot, dict) or not slot.get("enabled"):
            continue
        t = norm_time(slot.get("time"))
        if not t:
            continue
        hh, mm = (int(x) for x in t.split(":"))
        days = slot.get("days") or []
        for ahead in range(0, 8):          # 最多往后一周，够覆盖 days 过滤
            cand = (now + timedelta(days=ahead)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            if cand < now or (days and cand.weekday() not in days):
                continue
            if best is None or cand < best:
                best = cand
            break
    if best is None:
        return None
    return max(0, int((best - now).total_seconds()))


def stock(order: str = "old") -> list:
    """还没成功发出过的内容包（自动发布的货架）。见 douyin_publish.available。"""
    from app.services import douyin_publish

    return douyin_publish.available(order or "old", skip_given_up=True)


def fire_autopub(tid: str, now: datetime, fired: list[str], skipped: list[str],
                 force: bool = False) -> tuple[list[str], list[str]]:
    """自动发布入口：读配置 → 按 mode 分派。

    整段**持 autopub 锁**（读-改-写同一份 JSON）：调度线程与 HTTP 请求会同时改它，
    不然调度刚记的 `last_run` 会被一次保存覆盖 → 刚发过又发一次。
    """
    from app.services import douyin_store

    with douyin_store.autopub_lock():
        cfg = douyin_store.load_autopub()
        if not cfg.get("enabled"):
            return fired, skipped
        if cfg.get("mode") == "schedule":
            return fire(tid, cfg, now, fired, skipped, force)
        return fire_interval(tid, cfg, now, fired, skipped, force)


def fire(tid: str, cfg: dict, now: datetime,
         fired: list[str], skipped: list[str],
         force: bool = False) -> tuple[list[str], list[str]]:
    """定时发布的触发：只在配置的时刻（或迟到 ≤15 分钟内）发，每档发 N 条。

    防重靠 `last_shot`（`{时刻: 日期}`）—— tick 每分钟都跑，不记的话同一档一天
    会被触发几十次。`force=True` 供「保存并立即发一篇」用：立刻发 1 条，
    且**不写 last_shot**（否则会把当天那一档吃掉）。

    调用方须已持有 autopub 锁（`fire_autopub`）。
    """
    from app.services import douyin_store

    accounts = [str(x) for x in (cfg.get("account_ids") or [])]
    if not accounts:
        return fired, skipped

    ordered = stock(cfg.get("order") or "old")

    # 预补货：库存不足以支撑今天启用的档位数时先补一批，
    # 否则库存正好在某一档耗尽时，那一档会"跳过不发"。
    slots_today = sum(
        1 for s in (cfg.get("schedule") or [])
        if isinstance(s, dict) and s.get("enabled")
        and (not (s.get("days") or []) or now.weekday() in (s.get("days") or [])))
    if cfg.get("auto_gen") and len(ordered) < slots_today:
        _refill(tid, cfg, now, fired, skipped, tag="prefill")

    if force:
        if not ordered:
            _refill(tid, cfg, now, fired, skipped, tag="force")
            skipped.append(f"{tid}:douyin:schedule:force:库存空")
            return fired, skipped
        pick = ordered[0]
        _enqueue_publish(tid, [pick], accounts, cfg.get("publish_mode") or "rotate")
        fired.append(f"{tid}:douyin:schedule:force:{pick.get('id')}")
        douyin_store.save_autopub(cfg)
        return fired, skipped

    today = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute
    shots = dict(cfg.get("last_shot") or {})
    before = dict(shots)
    for slot in (cfg.get("schedule") or []):
        if not isinstance(slot, dict) or not slot.get("enabled"):
            continue
        t = str(slot.get("time") or "")
        if shots.get(t) == today:
            continue                      # 这一档今天已经发过了
        try:
            hh, mm = (int(x) for x in t.split(":"))
        except (TypeError, ValueError):
            continue
        late = now_min - (hh * 60 + mm)
        # 允许迟到 ≤15 分钟补发：tick 是 sleep(60) 循环、周期会随处理耗时漂移，
        # 严格等"分钟完全相等"会整分钟漏掉（漏了就一整天不再发）。
        if not (0 <= late <= SCHEDULE_CATCHUP_MIN):
            continue
        # 星期过滤：days 为空 = 每天；否则只在指定周几发（0=周一）
        days = slot.get("days") or []
        if days and now.weekday() not in days:
            continue
        # 每档可选指定账号；没指定就用卡片里勾选的那批轮流分配
        slot_accounts = [str(x) for x in (slot.get("account_ids") or [])] or accounts
        picks = ordered[:_clamp(slot.get("count"), 1, 10, 1)]
        if picks:
            _enqueue_publish(tid, picks, slot_accounts,
                             cfg.get("publish_mode") or "rotate")
            fired.append(f"{tid}:douyin:schedule:{t}:{len(picks)}")
            ordered = ordered[len(picks):]
        else:
            # 货架空了：先补产一包（下一档才有得发），本档照常记账不再重试
            _refill(tid, cfg, now, fired, skipped, tag=f"schedule:{t}")
        shots[t] = today

    if shots != before:
        cfg["last_shot"] = shots
        cfg["last_run"] = _stamp(now)
        douyin_store.save_autopub(cfg)
    return fired, skipped


def fire_interval(tid: str, cfg: dict, now: datetime, fired: list[str],
                  skipped: list[str], force: bool = False) -> tuple[list[str], list[str]]:
    """循环发布：到点发一篇未发过的内容包，另按 `gen_every_min` 补货。

    - `force=True`（「保存并立即发一篇」）忽略间隔立刻派发，并把 `last_run` 重置
      为现在 —— 否则下一次 tick 会以为"刚发过"而再等一整个间隔。
    - 补货时机：`gen_mode=interval` 按 `gen_every_min` 到点就补（这是页面上"每隔
      N 分钟自动发一篇库存内容包"的配套开关，改造前**没有任何代码读它**）；
      `gen_mode=stock` 只在货架空时补。

    调用方须已持有 autopub 锁（`fire_autopub`）。
    """
    from app.services import douyin_store

    accounts = [str(x) for x in (cfg.get("account_ids") or [])]
    if not accounts:
        skipped.append(f"{tid}:douyin:interval:未选账号")
        return fired, skipped

    every = _clamp(cfg.get("interval_min"), 5, 720, 30)
    last = _ts(cfg.get("last_run"))
    due = bool(force) or last is None or (now - last).total_seconds() >= every * 60

    shelf = stock(cfg.get("order") or "old")
    auto_gen = bool(cfg.get("auto_gen")) and cfg.get("gen_mode") != "off"
    if auto_gen and cfg.get("gen_mode") == "interval":
        lastg = _ts(cfg.get("last_gen"))
        span = _clamp(cfg.get("gen_every_min"), 5, 1440, 60) * 60
        gen_due = lastg is None or (now - lastg).total_seconds() >= span
    else:
        gen_due = not shelf

    if due and shelf:
        pick = shelf[0]
        _enqueue_publish(tid, [pick], accounts, cfg.get("publish_mode") or "rotate")
        fired.append(f"{tid}:douyin:publish:{pick.get('id')}")
    elif due:
        skipped.append(f"{tid}:douyin:interval:库存空")

    if auto_gen and gen_due:
        count = _refill(tid, cfg, now, fired, skipped, tag="interval")
        if count:
            cfg["last_gen"] = _stamp(now)

    if due:
        cfg["last_run"] = _stamp(now)
    if fired:
        douyin_store.save_autopub(cfg)
    return fired, skipped


def _enqueue_publish(tid: str, picks: list, accounts: list[str],
                     mode: str = "rotate") -> None:
    """把一批内容包交给发布队列。

    `mode` 来自 autopub 配置（`publish_mode`）：
    - `each`：一包发全部勾选的账号（用户要的"一包多发"，11 个号 = 11 条）；
    - `rotate`：一包只落一个号（老的自动发布口径，默认）。
    显式传值而不是落到默认值：默认值将来变动时，自动发布的行为不该跟着变。
    """
    jobs.enqueue("douyin.publish.batch",
                 {"pack_ids": [str(p.get("id")) for p in picks],
                  "account_ids": [str(x) for x in accounts],
                  "mode": mode},
                 tenant_id=tid, queue="gen")


def _refill(tid: str, cfg: dict, now: datetime, fired: list[str],
            skipped: list[str], tag: str) -> int:
    """补产一批内容包（`auto_gen` 关闭时不补，如实记 skip）。"""
    if not (cfg.get("auto_gen") and cfg.get("gen_mode") != "off"):
        skipped.append(f"{tid}:douyin:{tag}:库存空且未开启自动补货")
        return 0
    count = _clamp(cfg.get("gen_batch"), 1, 20, 5)
    jobs.enqueue("douyin.generate.batch", {"count": count},
                 tenant_id=tid, queue="gen")
    fired.append(f"{tid}:douyin:generate")
    return count