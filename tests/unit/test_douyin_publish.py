"""抖音图文「发布计划」的回归测试（2026-10-19 复查出的三个缺陷）。

改造前：
1. 发布计划是 pack × account 的**全量叉乘** —— 页面写"按账号轮流分配"，实际同一篇
   图文被每个号各发一遍；
2. 从不读 `Account.quota_update_sql()` —— 号打满了照发；
3. 挑库存只看"有没有成功账目"，失败的包被无限重试。

现在三条规则集中在 `services/douyin_publish.py`，手动发布与自动发布共用。

分发口径（2026-10-19 用户要求"能选一包发到所有号、也能选一包对一个号"，两种都要留）：
- `each`（默认，`rotate` 的反面）：每个勾选的号**各发一份**，跳过已发到过的号
  —— 所以"发到一半再点一次"是把剩下的号补齐，不会重复发；
- `rotate`：一个内容包只落**一个**号，多包多号轮流摊开。
用户不选就按 `each`（发布弹窗里的预览一直是"N 个内容包 × M 个账号"的多号口径）。
"""
from __future__ import annotations

from app.services import douyin_publish


def _acct(aid: str, **kw) -> dict:
    base = {"id": aid, "name": aid, "status": 1, "frozen": False,
            "daily_limit": 0, "remaining": None}
    base.update(kw)
    return base


def test_plan_rotate_is_one_to_one_not_cross_product():
    """rotate 口径下 3 个包 × 2 个号 = 3 条**一一配对**，不是 6 条叉乘。"""
    pairs, notes = douyin_publish.plan(
        ["1", "2", "3"], ["a1", "a2"], [_acct("a1"), _acct("a2")], ledger={}, mode="rotate")
    assert len(pairs) == 3, "每个包只发到一个号（旧实现是 3×2=6）"
    assert sorted(p for p, _ in pairs) == ["1", "2", "3"], "每个包只出现一次"
    assert not notes


def test_plan_rotates_across_accounts():
    """rotate 口径：两个号都要用上，不能全压在第一个号。"""
    pairs, _ = douyin_publish.plan(
        ["1", "2", "3", "4"], ["a1", "a2"], [_acct("a1"), _acct("a2")], ledger={}, mode="rotate")
    assert {a for _, a in pairs} == {"a1", "a2"}
    assert [a for _, a in pairs].count("a1") == 2


def test_plan_balances_with_ledger_history():
    """跨批次均衡：账本里 a1 已经成功发过 10 条 → 新包先给 a2。"""
    ledger = {"old": [{"ok": True, "account_id": "a1"}] * 10}
    pairs, _ = douyin_publish.plan(
        ["1"], ["a1", "a2"], [_acct("a1"), _acct("a2")], ledger=ledger, mode="rotate")
    assert pairs == [("1", "a2")]


def test_plan_skips_already_published():
    """已发过的包自动跳过，并在 notes 里如实说明（不静默少发）。"""
    ledger = {"1": [{"ok": True, "account_id": "a1"}]}
    pairs, notes = douyin_publish.plan(
        ["1", "2"], ["a1"], [_acct("a1")], ledger=ledger, mode="rotate")
    assert pairs == [("2", "a1")]
    assert any("已发布过" in n for n in notes)


def test_plan_probes_accounts_not_yet_tried():
    """同一个包上次发 a1 失败了 → 这次优先换 a2（失败的号可能正是元凶）。"""
    ledger = {"1": [{"ok": False, "account_id": "a1"}]}
    pairs, _ = douyin_publish.plan(
        ["1"], ["a1", "a2"], [_acct("a1"), _acct("a2")], ledger=ledger, mode="rotate")
    assert pairs == [("1", "a2")], "试过的号往后排"


def test_plan_drops_offline_accounts():
    """停用/掉线的号不进池，且如实记一条跳过原因。"""
    pairs, notes = douyin_publish.plan(
        ["1"], ["a1", "a2"], [_acct("a1", status=0), _acct("a2")], ledger={}, mode="rotate")
    assert pairs == [("1", "a2")]
    assert any("停用/掉线" in n for n in notes)


def test_plan_all_offline_reports_no_account():
    """号全不可用 → 一条都不发，且给出原因（而不是发到不可用的号上）。"""
    pairs, notes = douyin_publish.plan(
        ["1"], ["a1"], [_acct("a1", status=0)], ledger={}, mode="rotate")
    assert not pairs and notes


def test_plan_respects_daily_deficit():
    """当日额度只剩 1 → 这个号本批最多派 1 条。"""
    pairs, _ = douyin_publish.plan(
        ["1", "2", "3"], ["a1"], [_acct("a1", daily_limit=5, remaining=1)], ledger={}, mode="rotate")
    assert len(pairs) == 1, "额度剩余 1 只能派 1 条"


def test_plan_treats_zero_limit_as_unlimited():
    """`daily_limit = 0` 是**不限量**：不能因为 remaining=0 就把号踢出池子。"""
    pairs, _ = douyin_publish.plan(
        ["1", "2"], ["a1"], [_acct("a1", daily_limit=0, remaining=0)], ledger={}, mode="rotate")
    assert len(pairs) == 2, "不限额账号不该被 remaining=0 误伤"


def test_manual_plan_keeps_given_up_pack():
    """人工点发布就该照发 —— 放弃规则只约束自动发布。"""
    ledger = {"1": [{"ok": False, "account_id": "a1"}] * 3}
    auto, notes = douyin_publish.plan(
        ["1"], ["a1"], [_acct("a1")], skip_given_up=True, ledger=ledger, mode="rotate")
    assert not auto and any("已放弃" in n for n in notes)
    manual, _ = douyin_publish.plan(
        ["1"], ["a1"], [_acct("a1")], skip_given_up=False, ledger=ledger, mode="rotate")
    assert manual == [("1", "a1")], "用户明确要求重发时不该被拦"


# ── `each`：一包发到所有勾选的号（用户 2026-10-19 实测投诉的那条路径）──────

def test_plan_each_sends_pack_to_every_checked_account():
    """1 包 × 3 号 = 3 条 —— 用户勾了 12 个号却只发出去 1 条，就是这个口径缺失。"""
    pairs, notes = douyin_publish.plan(
        ["1"], ["a1", "a2", "a3"],
        [_acct("a1"), _acct("a2"), _acct("a3")], ledger={}, mode="each")
    assert pairs == [("1", "a1"), ("1", "a2"), ("1", "a3")], "勾选顺序 = 发送顺序"
    assert not notes


def test_plan_is_each_by_default():
    """不传 mode 时按默认口径走（页面上没选过口径的老请求不能变成只发 1 条）。"""
    pairs, _ = douyin_publish.plan(
        ["1", "2"], ["a1", "a2"], [_acct("a1"), _acct("a2")], ledger={})
    assert sorted(pairs) == [("1", "a1"), ("1", "a2"), ("2", "a1"), ("2", "a2")]


def test_plan_each_tops_up_only_missing_accounts():
    """each 口径按「包 × 号」判重：发过 a1 后再发一次，只补 a2（不重发 a1）。"""
    ledger = {"1": [{"ok": True, "account_id": "a1"}]}
    pairs, notes = douyin_publish.plan(
        ["1"], ["a1", "a2"], [_acct("a1"), _acct("a2")], ledger=ledger, mode="each")
    assert pairs == [("1", "a2")]
    assert any("只补没发过的号" in n for n in notes)


def test_plan_each_reports_when_all_accounts_done():
    """全发过 → 0 条，并给出原因（静默发 0 条是最难自查的故障）。"""
    ledger = {"1": [{"ok": True, "account_id": "a1"}]}
    pairs, notes = douyin_publish.plan(
        ["1"], ["a1"], [_acct("a1")], ledger=ledger, mode="each")
    assert not pairs
    assert any("无号可发" in n for n in notes)


def test_plan_each_partial_failure_does_not_block_other_accounts():
    """a1 失败不影响 a2：each 口径下失败只记在账本里，别的号照发。"""
    ledger = {"1": [{"ok": False, "account_id": "a1"}]}
    pairs, _ = douyin_publish.plan(
        ["1"], ["a1", "a2"], [_acct("a1"), _acct("a2")], ledger=ledger, mode="each")
    assert pairs == [("1", "a1"), ("1", "a2")], "失败过的号仍在候选里（人工重发就该再试）"


def test_plan_each_respects_quota_across_accounts():
    """额度在 each 口径下逐号算：a1 只剩 1 条 → 它只派 1 条，a2 照常。"""
    pairs, _ = douyin_publish.plan(
        ["1", "2"], ["a1", "a2"],
        [_acct("a1", daily_limit=5, remaining=1), _acct("a2")],
        ledger={}, mode="each")
    assert [a for _, a in pairs].count("a1") == 1
    assert [a for _, a in pairs].count("a2") == 2


def test_resolve_mode_prefers_request_then_config():
    """口径来源：请求体 > 配置里记着的上次选择 > 默认；都不认识就退回默认。"""
    assert douyin_publish.resolve_mode("rotate") == "rotate"
    assert douyin_publish.resolve_mode(None, {"publish_mode": "rotate"}) == "rotate"
    assert douyin_publish.resolve_mode("each", {"publish_mode": "rotate"}) == "each"
    assert douyin_publish.resolve_mode(None, {}) == douyin_publish.DEFAULT_MODE
    assert douyin_publish.resolve_mode("nonsense", {"publish_mode": "??"}) == "each"


# ── 账本解读 ────────────────────────────────────────────────────────────────

def test_state_of_resets_fails_after_success():
    """失败计数只数**最后一次成功之后**的 —— 修好配置后发成功就该翻篇。"""
    led = {"1": [{"ok": False}, {"ok": False}, {"ok": False}, {"ok": True}]}
    st = douyin_publish.state_of(led, "1")
    assert st["ok"] and st["fails"] == 0 and not st["given_up"]


def test_given_up_after_max_attempts():
    led = {"1": [{"ok": False}] * douyin_publish.MAX_AUTO_ATTEMPTS}
    assert douyin_publish.given_up(led, "1") is True
    assert douyin_publish.given_up(led, "9") is False, "没账目的包不算放弃"


def test_available_skips_given_up_and_orders(app, monkeypatch):
    """库存 = 没成功发过的包；自动发布侧排除已放弃的包，并按新旧排序。

    不排除的话，一个坏包会被每轮 tick 反复捞到队首，后面的真库存永远发不出去。
    """
    from app.services import douyin_publish as dp
    from app.services import douyin_store

    packs = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
    ledger = {"1": [{"ok": True}],                              # 已发 → 不在库存
              "2": [{"ok": False}] * dp.MAX_AUTO_ATTEMPTS,      # 已放弃
              "3": [{"ok": False}]}
    monkeypatch.setattr(douyin_store, "list_packs", lambda: packs)
    monkeypatch.setattr(douyin_store, "load_ledger", lambda: ledger)

    auto = [p["id"] for p in dp.available("old", skip_given_up=True)]
    assert auto == [3, 4]
    newest = [p["id"] for p in dp.available("new", skip_given_up=True)]
    assert newest == [4, 3]
    manual = [p["id"] for p in dp.available("old", skip_given_up=False)]
    assert manual == [2, 3, 4], "人工发布的货架上放弃包也在"


# ── 当日额度：真库原子占用 ──────────────────────────────────────────────────

def _add_account(app, **kw):
    from app.extensions import db
    from app.models import Account
    from app.timeutil import now_local

    row = Account(id=kw.pop("id", "a1"), tenant_id=kw.pop("tenant_id", "t_alpha"),
                  platform=kw.pop("platform", "douyin_imgtext"),
                  name=kw.pop("name", "测试号"),
                  # SQLite 的 Date 列只收 date 对象（不收字符串）
                  quota_date=kw.pop("quota_date", now_local().date()), **kw)
    db.session.add(row)
    db.session.commit()
    return row


def test_quota_claim_is_atomic_and_counts_down(app):
    """额度按"尝试"扣：第 1 次放行、第 2 次打回（同一张表、同一条 SQL）。"""
    from app.tenancy import tenant_scope

    with app.app_context():
        _add_account(app, id="a1", daily_limit=1)
        with tenant_scope("t_alpha"):
            assert douyin_publish.quota_claim("a1") is None
            assert douyin_publish.quota_claim("a1") == "quota", "第 2 次应被额度挡下"


def test_quota_claim_passes_unlimited_and_unknown(app):
    """不限量（daily_limit=0）与查不到本地行的账号直接放行。"""
    from app.tenancy import tenant_scope

    with app.app_context():
        _add_account(app, id="a2", daily_limit=0)
        with tenant_scope("t_alpha"):
            assert douyin_publish.quota_claim("a2") is None
            assert douyin_publish.quota_claim("yx-nonlocal") is None, "纯云账号不限额"


def test_quota_claim_resolves_cloud_account_via_yxer_id(app):
    """页面上的云账号 ID 是 `yx-<云id>`，额度挂在同步进来的本地行上。"""
    from app.tenancy import tenant_scope

    with app.app_context():
        _add_account(app, id="local-1", yxer_id="cloud9", daily_limit=1)
        with tenant_scope("t_alpha"):
            assert douyin_publish.quota_claim("yx-cloud9") is None
            assert douyin_publish.quota_claim("yx-cloud9") == "quota"