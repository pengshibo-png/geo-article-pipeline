"""写稿自愈环（编排层）：审稿不过就重写；重写额度用尽仍**交付可用文章**。

背景（2026-10-16 定位，31 篇实测）：
- 19 篇(61%) 卡在 B 段 —— 真模型漏一个字段就被冻结契约一票否决（格式误杀）；
- 4 篇(13%) 卡在 C 段 —— **全部**是 verdict=「建议重写」，而 C 段按冻结规则
  直接抛错终止，于是整篇作废、零重试；
- 审稿打分中位数只有 63，而契约规定 score<60 必须判「建议重写」
  —— 等于"分数贴着 60 漂，掉下去就必死"。

三层自愈（全部**有界**，见下方预算）：
1. B 段格式问题 → 把具体错误项喂回去重试（review.py）；
2. 判「建议重写」→ 回 A 段重写一篇新的，并把审稿意见注入 A 段 Prompt；
3. 上述都用尽 / 后续阶段硬失败 → **降级交付**：尽力修订一次，产出可用文章，
   并**如实标注"未通过审核"**（审稿结论原样留在 02_review.json，绝不伪装成过审）。

## 硬预算（token 保护）

单篇文章的 LLM 调用次数上限：

    每轮 = A(1) + B(≤1+2 次格式重试) + C(1) = 最多 5 次
    轮数 = 1 + REWRITE_ROUNDS = 3 轮                      → 最多 15 次
    降级交付的尽力修订                                    → 最多 +1 次
    ────────────────────────────────────────────────  上限 16 次，典型 3~4 次

循环全部是 `for` + 常量上界，**不存在 while-True**；额度用尽必定走降级交付收尾。

## 交付保证

只要 A 段产出了初稿，这篇文章**一定会有 03_final.html**（正常过审 / 降级交付）。
只有 A 段本身失败（品牌不合规、LLM 全挂、空正文）才会真的没有文章。

兜底动作的实现（降级交付 / 回退初稿 / 重写前快照）在 `writer/recover.py`
（A3 行数预算拆出）；本模块只剩"什么时候该走哪一步"的编排。
"""
from __future__ import annotations

from app.logging import get_logger
from app.platform import storage
from app.services.writer.recover import (
    _deliver_degraded,
    _notify,
    _read_review,
    _restore_snapshot,
    _revert_to_draft,
    _snapshot_for_rewrite,
    _title_of,
)

log = get_logger(__name__)

# 回 A 段重写的最大轮数（首轮之外）。总轮数 = REWRITE_ROUNDS + 1。
REWRITE_ROUNDS = 2


def rewrite_note(review: dict) -> str:
    """把上一轮审稿意见整理成 A 段的硬约束块（注入 Prompt 末尾）。"""
    score = review.get("overall_score")
    lines = [f"上一轮审核未通过：verdict=「{review.get('verdict')}」，得分 {score}。"]
    summary = str(review.get("summary") or "").strip()
    if summary:
        lines.append(f"审稿总评：{summary[:200]}")
    lines.append("以下问题必须逐条解决。请重写**全文**，不要沿用上一版的写法与句式：")
    n = 0
    for it in (review.get("issues") or []):
        if not isinstance(it, dict):
            continue
        n += 1
        if n > 12:
            break
        lines.append(
            f"- [{it.get('severity')}][{it.get('dimension')}] "
            f"问题：{str(it.get('problem') or '')[:110]}；"
            f"改法：{str(it.get('suggestion') or '')[:110]}")
    if n == 0:
        lines.append("-（审稿未给出可执行条目，请整体提升信息密度与品牌融入质量）")
    return "【上一轮审核意见·本轮必须逐条规避】\n" + "\n".join(lines)


def run_with_rewrite(payload: dict, notify=None) -> dict:
    """A→B→C 自愈环。返回 `{"dir","stages","rounds","rewrites","degraded"}`。

    `stages` 是**最终那一轮**的三段结果（形状与既有 `article.pipeline` 契约一致）。
    `notify`：可选回调，用于把进展打到页面日志（批量写稿传 gen_log）。
    """
    from app.services.writer.pipeline import generate_article, slugify  # 延迟：防循环导入
    from app.services.writer.review import review_article, review_final, revise_article
    from app.errors import RewriteRequired

    base = payload.get("base")
    contact_mode = payload.get("contact_mode", "revise")
    title = str(payload.get("title") or "")
    dir_name = payload.get("dir")
    note = ""

    for round_no in range(1, REWRITE_ROUNDS + 2):
        stages: list = []
        snap: dict = {}
        if round_no > 1:
            # 目录名 = 标题、跨轮不变；不清掉上一轮产物的话 generate_article
            # 会按"初稿存在即跳过"的断点续跑规则直接返回 skipped，重写无从发生。
            # 只在目标目录与当前一致时清（不一致说明是自定义目录的续跑场景，
            # 那就让新目录接管，不动旧目录里的东西）。
            if slugify(title) == dir_name:
                # 先备份再删：上一轮的初稿与审稿此刻是唯一副本，而下面的
                # generate_article 一旦失败，文章目录就空了（见 _snapshot_for_rewrite）。
                snap = _snapshot_for_rewrite(dir_name, base)
                # FINAL_REVIEW（04）也要清：否则重写后档案页显示的仍是**上一版定稿**
                # 的分数，与新的 03_final.html 完全不是同一篇（storage.py 注释里
                # 明确要求"回 A 段重写时一并清掉"）。
                storage.remove_article_files(dir_name, storage.DRAFT, storage.REVIEW,
                                             storage.FINAL_REVIEW, base=base)
            _notify(notify,
                    f"↻ 第 {round_no}/{REWRITE_ROUNDS + 1} 轮重写「{title}」："
                    "按上一轮审稿意见重做初稿")
        if round_no > 1 or not dir_name:
            try:
                a = generate_article(title_text=title or None, contact_mode=contact_mode,
                                     angle_id=payload.get("angle"), base=base,
                                     rewrite_note=note)
            except Exception as exc:  # noqa: BLE001 - 见下：绝不让整篇消失
                # 重写轮的初稿失败**不能**连带毁掉上一轮成果：先把备份写回去，
                # 再走降级交付（上一轮初稿本就是 A 段按完整规范写出的成稿，可用）。
                log.error("article_rewrite_draft_failed", extra={
                    "event": "article_rewrite_draft_failed", "dir": dir_name,
                    "round": round_no,
                    "err": f"{type(exc).__name__}: {str(exc)[:200]}"})
                if not snap:
                    raise          # 没删任何东西（自定义目录续跑）→ 维持原行为
                _restore_snapshot(dir_name, snap, base)
                return _deliver_degraded(
                    dir_name, base, contact_mode, _read_review(dir_name, base),
                    stages, notify,
                    f"第 {round_no} 轮重写初稿生成失败（{type(exc).__name__}），"
                    "已回滚上一轮初稿")
            stages.append(a)
            dir_name = a["dir"]
            title = title or str(a.get("title") or dir_name)

        try:
            stages.append(review_article(dir_name, contact_mode=contact_mode, base=base))
        except Exception as exc:  # noqa: BLE001 - 已有初稿 → 降级交付，不作废
            log.error("article_review_failed", extra={
                "event": "article_review_failed", "dir": dir_name,
                "err": f"{type(exc).__name__}: {str(exc)[:200]}"})
            return _deliver_degraded(dir_name, base, contact_mode, {}, stages, notify,
                                     f"审稿未通过（{type(exc).__name__}）")

        try:
            stages.append(revise_article(dir_name, contact_mode=contact_mode, base=base))
        except RewriteRequired as exc:
            review = _read_review(dir_name, base)
            score = review.get("overall_score")
            if round_no > REWRITE_ROUNDS:
                # 重写额度用尽：不再作废 —— 尽力交付一篇可用文章（但如实标注）。
                log.error("article_rewrite_exhausted", extra={
                    "event": "article_rewrite_exhausted", "dir": dir_name,
                    "rounds": round_no, "score": score, "err": str(exc)[:200]})
                return _deliver_degraded(
                    dir_name, base, contact_mode, review, stages, notify,
                    f"自动重写 {REWRITE_ROUNDS} 轮后审稿仍判「建议重写」（得分 {score}）")
            if not title:
                title = _title_of(dir_name)
            note = rewrite_note(review)
            log.warning("article_rewrite_scheduled", extra={
                "event": "article_rewrite_scheduled", "dir": dir_name,
                "round": round_no, "score": score})
            _notify(notify, f"↺ 审稿判定「建议重写」（得分 {score}）→ 自动回 A 段重写"
                            f"（第 {round_no + 1}/{REWRITE_ROUNDS + 1} 轮）")
            continue
        except Exception as exc:  # noqa: BLE001 - 定稿失败 → 降级交付，不作废
            log.error("article_revise_failed", extra={
                "event": "article_revise_failed", "dir": dir_name,
                "err": f"{type(exc).__name__}: {str(exc)[:200]}"})
            return _deliver_degraded(dir_name, base, contact_mode,
                                     _read_review(dir_name, base), stages, notify,
                                     f"定稿阶段失败（{type(exc).__name__}）")
        out: dict = {"dir": dir_name, "stages": stages, "rounds": round_no,
                     "rewrites": round_no - 1, "degraded": False}
        # C 段改完**再复评一次**：定稿才是真正交付的东西，必须有自己的分数。
        # 例外：`source == "draft-reused"` 表示审稿判「可直接发布」，定稿就是初稿
        # 本身 —— 复评等于把同一篇文章再审一遍（纯白烧一次调用），直接沿用 02 分数。
        c_stage = stages[-1] if stages else {}
        fr: dict = {}
        if str(c_stage.get("source") or "") == "draft-reused":
            rev2 = _read_review(dir_name, base)
            fr = {"verdict": rev2.get("verdict"),
                  "score": rev2.get("overall_score")}
            out["final_review_reused"] = True
        else:
            try:
                fr = review_final(dir_name, contact_mode=contact_mode, base=base)
            except Exception as exc:  # noqa: BLE001 - 复评失败不拦交付，只留日志
                log.warning("article_final_review_failed", extra={
                    "event": "article_final_review_failed", "dir": dir_name,
                    "err": f"{type(exc).__name__}: {str(exc)[:200]}"})
                fr = {}
        if fr:
            out["final_verdict"] = fr.get("verdict")
            out["final_score"] = fr.get("score")
            if fr.get("verdict") == "建议重写":
                # 改完反而更差（<60）→ 回退用初稿交付，别把改坏的版本发出去。
                # 回退会替换磁盘上的 03_final.html，所以动作必须记账、且可能被拒
                # （已发布的稿子不覆盖，见 _revert_to_draft）。
                action = _revert_to_draft(dir_name, base)
                out["final_revert_action"] = action
                if action == "reverted":
                    out["final_reverted"] = True
                    # 交付的是**回退后的初稿**，不是复评通过的那版 —— 必须标降级，
                    # 否则编排层打「✅ 完成」，用户根本看不出这是回退稿。
                    out["degraded"] = True
                    out["degrade_reason"] = (
                        f"定稿复评 {fr.get('score')} 分不合格，已回退为初稿交付")
                log.error("article_final_review_reverted", extra={
                    "event": "article_final_review_reverted", "dir": dir_name,
                    "score": fr.get("score"), "action": action})
                _notify(notify, "⚠ 定稿复评不合格（得分 "
                        f"{fr.get('score')}）："
                        + ("已回退为初稿交付" if action == "reverted"
                           else f"**未回退**（{action}）—— 请人工复核 03_final.html"))
        return out

    raise RuntimeError("重写环异常退出")   # pragma: no cover - 循环内必 return
