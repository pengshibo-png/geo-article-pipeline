"""抖音图文的**发布编排**：把"选中的包 + 选中的号"编成一一配对的发布计划。

2026-10-19 复查出来的三个缺陷，根因都在"发布计划"这一步：

1. **轮流分配是假的**：原来是 pack × account 的**全量叉乘**（3 个包 × 2 个号 = 6 次
   发布），同一篇图文被每个号都发了一遍 —— 而页面写的是"内容包按账号轮流分配，
   已发过的自动跳过"。平台侧判重降权，用户侧看到的是重复内容。
2. **当日额度从没被读过**：`Account.quota_update_sql()` 只有文章发布一侧调用，
   抖音这边 `used_today` 永远是 0 —— 卡片上"剩余 N 条"永远显示满额，号打满了
   也照发（再被平台打回一次）。
3. **失败的包无限重试**：挑库存只看"有没有 ok 账目"，失败的包每 5 分钟被重发一次，
   永远发不出去也永远不放弃（实测一个号连撞 7 次"未设置代理"）。

手动发布（`DouyinService.publish`）与自动发布（`douyin_jobs.dy_publish_job` /
`douyin_schedule`）**共用本模块的口径**，否则"页面说轮流、实际叉乘"还会回来。
"""
from __future__ import annotations

from app.logging import get_logger

log = get_logger(__name__)

# 自动发布对同一个内容包的连续失败上限：到顶就"放弃"（人工重发不受此限）。
# 设 3 是因为失败原因几乎都是账号级/配置级（未设代理、Key 失效、图挂了），
# 换个时刻重试并不会变好，而每试一次都要白传一批 5MB 的图。
MAX_AUTO_ATTEMPTS = 3

# 分发口径（两种都要，由用户显式选）：
# - `each`  ：每个勾选的号**各发一份**（1 包 × 12 号 = 12 条）。同一内容铺满矩阵账号，
#             代价是平台可能按重复内容判重降权 —— 所以它必须是**用户主动选的**。
# - `rotate`：**轮流分配**，一个内容包只发到**一个**号（1 包 × 12 号 = 1 条），
#             多包多号时自动摊开，不重复铺。
# 默认取 `each`：发布弹窗里的预览一直是"N个内容包 × M个账号，每号约X包"（多号分发
# 口径），页面上那句"轮流分配"的旧提示与它自相矛盾。2026-10-19 用户实测投诉的正是
# 这个矛盾：勾了所有号只发出去 1 条。
MODES = ("each", "rotate")
DEFAULT_MODE = "each"


def resolve_mode(body_mode, cfg: dict | None = None) -> str:
    """把（请求里的、配置里的）分发口径归一成 `each` / `rotate`。

    两处来源：手工发布由页面在请求体里传 `mode`；配置里存的 `publish_mode` 是
    "上次选过的口径"。都不认识就退回默认，**绝不静默按别的口径发**。
    """
    raw = str(body_mode or (cfg or {}).get("publish_mode") or "").strip()
    return raw if raw in MODES else DEFAULT_MODE


def state_of(ledger: dict, pack_id) -> dict:
    """一个内容包的账本快照。

    `fails` 只数**最后一次成功之后**的失败：修好配置后重发成功，历史失败不该
    继续累加（否则包会一直被当成"已放弃"）。
    """
    records = ledger.get(str(pack_id)) or []
    ok = False
    fails = 0
    accounts: set[str] = set()
    ok_accounts: set[str] = set()
    for r in records:
        if not isinstance(r, dict):
            continue
        aid = str(r.get("account_id") or "")
        if aid:
            accounts.add(aid)
        if r.get("ok"):
            ok, fails = True, 0
            if aid:
                ok_accounts.add(aid)
        else:
            fails += 1
    return {"ok": ok, "fails": fails, "accounts": accounts,
            "ok_accounts": ok_accounts,
            "given_up": (not ok) and fails >= MAX_AUTO_ATTEMPTS}


def is_published(ledger: dict, pack_id) -> bool:
    return state_of(ledger, pack_id)["ok"]


def given_up(ledger: dict, pack_id, limit: int = MAX_AUTO_ATTEMPTS) -> bool:
    """自动发布是否该放弃这个包（连续失败到上限且从未成功过）。"""
    st = state_of(ledger, pack_id)
    return (not st["ok"]) and st["fails"] >= limit


def available(order: str = "old", *, skip_given_up: bool = True) -> list:
    """还没成功发布过的内容包（自动发布的"库存"）。

    `skip_given_up=True` 时排除已连续失败到上限的包 —— 否则那批坏包会被每轮
    tick 反复挑中，永远占着队首（真正的库存排在后面永远发不出去）。
    人工发布走 `plan(skip_given_up=False)`：用户明确点了发布，就该照发。
    """
    from app.services import douyin_store

    ledger = douyin_store.load_ledger()
    out = []
    for p in douyin_store.list_packs():
        pid = str(p.get("id"))
        st = state_of(ledger, pid)
        if st["ok"]:
            continue
        if skip_given_up and st["given_up"]:
            continue
        out.append(p)
    return out if order == "old" else list(reversed(out))


def plan(pack_ids, account_ids, accounts, *, skip_given_up: bool = False,
         ledger: dict | None = None,
         mode: str = DEFAULT_MODE) -> tuple[list[tuple[str, str]], list[str]]:
    """把内容包与账号编成发布计划（**逐对** (内容包, 账号)）。

    两种口径（`mode`，见 `MODES`）：
    - `each`：每个包发到**每一个**勾选的号各一份（1 包 × 12 号 = 12 条）；
    - `rotate`：每个包只发到**一个**号（1 包 × 12 号 = 1 条），多包多号时轮流摊开。

    共有规则（对页面上的承诺负责）：
    - 已发过的**自动跳过** —— `each` 按「包×号」为单位（该号发过就只跳该号，
      其余号照发，于是"把这条补发到其它号"一点就能补上）；`rotate` 按包为单位
      （发过一次就不再占号）；
    - 停用/掉线的号不进池；已知当日剩余额度不足的号不进池（额度本批内不会超发）；
    - `skip_given_up=True`（自动发布）时排除连续失败到上限的包。

    返回 `(pairs, notes)`：`notes` 是逐条跳过原因，调用方如实回显 —— 静默少发几条
    是用户最难自查的一类故障（"我以为发了 12 条、结果发了 1 条"就是这么来的）。
    """
    from app.services import douyin_store

    mode = mode if mode in MODES else DEFAULT_MODE
    led = ledger if ledger is not None else douyin_store.load_ledger()
    live = {str(a.get("id")): a for a in (accounts or []) if a.get("status") == 1}
    asked = [str(x) for x in account_ids if str(x) in live]
    dropped = [str(x) for x in account_ids if str(x) not in live]
    load = {aid: _load(led, aid) for aid in asked}
    # ★ 本批已派给各号的条数（额度闸门专用，恒从 0 起算）。
    # `load` 是**全历史**成功条数（跨批次摊开用的负载权重），两者时间口径不同，
    # 绝不能相减：曾把 `load` 塞进额度闸门（deficit - load > 0），于是每发一条
    # deficit 减 1、load 加 1，差值**每次掉 2** —— 号发得越多越会被误判成
    # "当日额度已满"而被踢出发布池。实测 2026-10-21：07:20 还能派 11 个号，
    # 到 16:05 只剩 1 个（探城小队长），当天最后一档直接发 0 个号。
    assigned = {aid: 0 for aid in asked}
    notes: list[str] = []
    if dropped:
        notes.append(f"{len(dropped)} 个账号停用/掉线，已跳过：{', '.join(dropped[:3])}"
                     + ("…" if len(dropped) > 3 else ""))

    pairs: list[tuple[str, str]] = []
    deficit = {aid: _remaining(live[aid]) for aid in asked}
    for pid in [str(x) for x in pack_ids]:
        st = state_of(led, pid)
        if skip_given_up and st["given_up"]:
            notes.append(f"#{pid} 连续失败 {st['fails']} 次，已放弃（可手动重发）")
            continue
        # 已经排满的号不再进候选（`deficit` 扣掉**本批**已派的条数）
        cands = [aid for aid in asked
                 if (deficit[aid] is None or deficit[aid] - assigned[aid] > 0)]
        if mode == "each":
            if st["ok_accounts"]:
                notes.append(f"#{pid} 已在 {len(st['ok_accounts'])} 个号发过，"
                             "本次只补没发过的号")
            fresh = [aid for aid in cands if aid not in st["ok_accounts"]]
            if not fresh:
                notes.append(f"#{pid} 无号可发（勾选的号都已发过 / 停用 / 当日额度已满）")
                continue
            for aid in fresh:                  # 每个号各发一份（勾选顺序 = 发送顺序）
                pairs.append((pid, aid))
                load[aid] += 1
                assigned[aid] += 1
            continue
        # rotate：一个包只占一个号。优先挑**还没试过这个包**的号（试过又失败的多半
        # 还是同一个原因），其次挑账本负载最轻的（跨批次摊开），最后按勾选顺序。
        if st["ok"]:
            notes.append(f"#{pid} 已发布过，跳过")
            continue
        fresh = [aid for aid in cands if aid not in st["accounts"]] or cands
        if not fresh:
            notes.append(f"#{pid} 无可用账号（账号停用或当日额度已满）")
            continue
        pick = min(fresh, key=lambda a: (load[a], asked.index(a)))
        pairs.append((pid, pick))
        load[pick] += 1
        assigned[pick] += 1
    return pairs, notes


def _load(ledger: dict, account_id: str) -> int:
    """该号在账本里已成功发出的条数（跨批次均衡用）。"""
    n = 0
    for records in ledger.values():
        for r in records:
            if isinstance(r, dict) and r.get("ok") \
                    and str(r.get("account_id") or "") == account_id:
                n += 1
    return n


def _remaining(account: dict):
    """账号的当日剩余额度；`None` = 不限额（`daily_limit = 0` 的语义）。

    页面上的 `remaining` 是 `daily_limit - used_today`，而 daily_limit=0 表示
    **不限量** —— 直接相减会得到负数，把不限额的号当成"已打满"踢出池子。
    """
    if "daily_limit" in account:
        try:
            if int(account.get("daily_limit") or 0) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        try:
            return max(0, int(account.get("remaining") or 0))
        except (TypeError, ValueError):
            return 0
    rem = account.get("remaining")
    if rem is None:
        return None
    try:
        return max(0, int(rem))
    except (TypeError, ValueError):
        return None


# ── 额度：发布前原子占用（与文章发布同一把锁、同一张表）──────────────────────

def local_account(account_id: str):
    """把账号 ID 解析成本地 `Account` 行（额度挂在本地行上）。

    `yx-xxx` 是云账号在页面上的 ID（= `yxer-` + 云账号 id），额度记在同步进来的
    本地行上；纯云账号没有本地行 → 不限额。
    """
    from app.models import Account
    from app.tenancy import TenantScopedRepository

    repo = TenantScopedRepository(Account)
    aid = str(account_id)
    if not aid.startswith("yx-"):
        return repo.query().filter(Account.id == aid).first()
    yid = aid[3:]
    return (repo.query().filter(Account.yxer_id == yid).first()
            or repo.query().filter(Account.id == aid).first())


def quota_claim(account_id: str) -> str | None:
    """发布前**原子**占用一次当日额度。

    返回 `None` = 放行；`"quota"` = 当日额度已用尽（调用方跳过这个号并如实提示
    "次日恢复"）。不限额（无本地行 / `daily_limit = 0`）直接放行，不做无意义的累加。

    即写即交：这一步后面跟着几十秒到几分钟的网络上图，事务持有写锁会把同进程的
    其他作业一起堵死（与 `writer/pipeline.py` 同一教训）。
    """
    from sqlalchemy import text

    from app.extensions import db
    from app.models import Account
    from app.timeutil import natural_day_string

    row = local_account(account_id)
    if row is None or int(row.daily_limit or 0) <= 0:
        return None
    res = db.session.execute(text(Account.quota_update_sql()), {
        "id": row.id, "tenant_id": row.tenant_id, "today": natural_day_string()})
    db.session.commit()
    return None if res.rowcount else "quota"