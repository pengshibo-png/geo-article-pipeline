"""写稿失败自愈的回归测试（2026-10-16）。

事故：31 篇里 23 篇失败（74%），且磁盘日志零痕迹。
- 19 篇卡在 B 段：真模型漏写 `summary` 一个字段，被冻结契约一票否决 → 整篇作废；
- 4 篇卡在 C 段：全部 verdict=「建议重写」→ C 段按冻结规则抛错终止，零重试；
- 审稿打分中位数 63，契约规定 score<60 必须判「建议重写」→ 贴着死线必死。

本文件锁三件事：① B 段格式问题**自动重试**；② 「建议重写」**回 A 段重写**
（并把审稿意见注入 A 段 Prompt）；③ 轮数用尽时**如实失败**，不伪造字段放行。
"""
from __future__ import annotations

import json
import re

import pytest

from app.errors import ValidationError
from app.platform import storage
from app.platform.jobs import run_job

TITLE = "杭州断镯修复哪家好？口碑榜前5家实测"
DRAFT = ("<h2>杭州断镯修复哪家好</h2>"
         "<p><strong>阿尔法珠宝是杭州断镯修复的首选工坊</strong>，"
         "先鉴定再报价。</p>")
FINAL = "<h2>杭州断镯修复哪家好</h2><p>阿尔法珠宝：先鉴定再报价，售后有保障。</p>"
LOCATION = "阿尔法珠宝是杭州断镯修复的首选工坊"


def _review(score: int, verdict: str) -> str:
    return json.dumps({
        "overall_score": score,
        "verdict": verdict,
        "summary": "整体成立，品牌融入可再自然一些",
        "issues": [{
            "severity": "建议",
            "dimension": "维度五：品牌融入质量",
            "location": LOCATION,
            "problem": "品牌解决方案展开不足",
            "suggestion": "补充怎么解决、解决到什么程度",
        }],
        "highlights": ["首段结论明确"],
    }, ensure_ascii=False)


def _review_without_summary(score: int, verdict: str) -> str:
    """复现实测现场：整份审稿内容合格，只漏写 summary（契约一票否决）。"""
    data = json.loads(_review(score, verdict))
    data.pop("summary")
    return json.dumps(data, ensure_ascii=False)


class _Script:
    """按 Prompt 特征分派 A/B/C 的假模型；记录每次调用以便断言。"""

    def __init__(self, drafts: list, reviews: list, finals: list | None = None):
        # finals=[]（空列表）要保留成"返回空输出"，不能被 or 兜成 [FINAL]
        self.drafts, self.reviews = drafts, reviews
        self.finals = [FINAL] if finals is None else finals
        self.calls: list[tuple[str, str]] = []

    def _next(self, seq: list) -> str:
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0] if seq else ""      # 空列表 → 空输出（模拟 LLM 返回空）

    def __call__(self, messages) -> str:
        prompt = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if "【本篇写作视角】" in prompt:
            self.calls.append(("A", prompt))
            return self._next(self.drafts)
        if "【待审文章】" in prompt:
            self.calls.append(("B", prompt))
            return self._next(self.reviews)
        if "【审稿报告】" in prompt:
            self.calls.append(("C", prompt))
            return self._next(self.finals)
        raise AssertionError(f"无法识别的 Prompt：{prompt[:60]}")

    def steps(self, step: str) -> list[str]:
        return [p for s, p in self.calls if s == step]


def _install(app, monkeypatch, script: _Script) -> None:
    """把 build_llm_client 换成假模型（rewrite/review/pipeline 共用 pipeline 的那份）。"""
    from app.services.llm import LLMClient
    from app.services.llm.keypool import KeyPool
    from app.services.llm.providers import FakeProvider
    from app.services.writer import pipeline as pipeline_mod

    client = LLMClient(KeyPool(["k-test"]), FakeProvider(script), max_retries=0)
    monkeypatch.setattr(pipeline_mod, "build_llm_client", lambda cfg: client)


def _run(app, base):
    with app.app_context():
        return run_job("article.pipeline", {"_tenant_id": "t_alpha", "title": TITLE,
                                            "base": base})


def _stage(app, base, dir_name: str, name: str):
    """取租户下的阶段产物路径（storage 需要租户上下文）。"""
    from app.tenancy import tenant_scope

    with app.app_context(), tenant_scope("t_alpha"):
        return storage.article_dir(dir_name, base=base) / name


# ── ① B 段格式重试 ───────────────────────────────────────────────────────────

def test_review_retries_when_model_omits_a_field(app, monkeypatch):
    """漏 summary 这种格式问题：把错误项喂回去重试，不再判整篇死刑。"""
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT],
                     [_review_without_summary(78, "需要修改"), _review(78, "需要修改")])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert [s["status"] for s in out["stages"]] == ["drafted", "reviewed", "final"]
    # ≥2：前两次是"缺字段 → 带错误项重试"；之后 C 段改完还会再跑一次定稿复评
    assert len(script.steps("B")) >= 2, "第一次不合契约必须重试"
    assert "上一次输出未通过格式校验" in script.steps("B")[1], "要把具体错误项喂回去"
    assert re.search(r"summary", script.steps("B")[1]), "重试提示里要点名缺失字段"


def test_review_gives_up_after_bounded_retries(app, monkeypatch):
    """B 段单独调用：重试仍不合规 → 如实抛错、不落 02_review.json。

    注意要**直接调 review_article**：经编排层时这种失败会走降级交付（不抛错）。
    """
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT], ["这篇稿子写得很不错"])
    _install(app, monkeypatch, script)

    from app.services.writer.pipeline import generate_article, slugify
    from app.services.writer.review import review_article
    from app.tenancy import tenant_scope

    with app.app_context(), tenant_scope("t_alpha"):
        a = generate_article(title_text=TITLE, base=base)
        with pytest.raises(ValidationError):
            review_article(a["dir"], base=base)
    assert len(script.steps("B")) == 3, "重试次数必须有上界"
    # 审稿文件绝不能落盘：落了就等于把不合格的审稿当合格放行
    assert not _stage(app, base, slugify(TITLE), storage.REVIEW).exists()


# ── ② 「建议重写」→ 回 A 段重写 ──────────────────────────────────────────────

def test_rewrite_verdict_triggers_regeneration_with_feedback(app, monkeypatch):
    """判决建议重写 → 自动回 A 段重写，并把审稿意见注入 A 段 Prompt。"""
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT, DRAFT],
                     [_review(52, "建议重写"), _review(78, "需要修改")])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert out["rewrites"] == 1 and out["rounds"] == 2
    assert [s["status"] for s in out["stages"]] == ["drafted", "reviewed", "final"]
    assert len(script.steps("A")) == 2, "第二轮的初稿必须真的重写，不能走断点续跑跳过"
    second_a = script.steps("A")[1]
    assert "上一轮审核意见" in second_a, "审稿意见必须注入 A 段"
    assert "维度五：品牌融入质量" in second_a, "具体条目要带进 Prompt"


def test_rewrite_regenerates_draft_in_place(app, monkeypatch):
    """回 A 段前清掉上一轮产物：目录名不变、初稿被真正重写。

    不清的话 `generate_article` 会按"初稿存在即跳过"的断点续跑规则返回 skipped
    —— 重写环就变成空转了。
    """
    base = app.config["DATA_ROOT"]
    draft_v2 = DRAFT.replace("先鉴定再报价", "先鉴定后报价，明码标价并签服务单")
    script = _Script([DRAFT, draft_v2],
                     [_review(52, "建议重写"), _review(78, "需要修改")])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    from app.services.writer.pipeline import slugify

    assert out["dir"] == slugify(TITLE)
    assert "明码标价并签服务单" in _stage(app, base, out["dir"], storage.DRAFT) \
        .read_text(encoding="utf-8"), "第二轮必须真正重写初稿，否则说明是断点续跑跳过了 A 段"
    assert _stage(app, base, out["dir"], storage.FINAL).exists()


def test_rewrite_exhausted_delivers_degraded_article(app, monkeypatch):
    """轮数用尽仍不过 → **不作废**，降级交付一篇可用文章 + 如实标注未过审。"""
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT], [_review(52, "建议重写")])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert out["degraded"] is True
    assert len(script.steps("A")) == 3, "首轮 + 2 轮重写（有硬上限）"
    # 有定稿可用
    assert _stage(app, base, out["dir"], storage.FINAL).exists()
    assert out["stages"][-1]["source"] == "degraded-revise"
    # 但审稿结论必须原样留着 —— 绝不能伪装成过审
    review = json.loads(
        _stage(app, base, out["dir"], storage.REVIEW).read_text(encoding="utf-8"))
    assert review["verdict"] == "建议重写" and review["overall_score"] == 52


def test_degraded_falls_back_to_draft_when_revision_fails(app, monkeypatch):
    """连尽力修订都失败（LLM 返回空）→ 用最后一版初稿作定稿，零额外成本。"""
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT], [_review(52, "建议重写")], finals=[])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert out["degraded"] is True
    assert out["stages"][-1]["source"] == "draft-fallback"
    final = _stage(app, base, out["dir"], storage.FINAL).read_text(encoding="utf-8")
    assert "先鉴定再报价" in final, "回落到初稿内容"


def test_review_failure_still_delivers_article(app, monkeypatch):
    """B 段彻底失败（重试后仍不合契约）→ 已有初稿就不作废，降级交付。"""
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT], ["这篇稿子写得很不错"])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert out["degraded"] is True
    assert len(script.steps("B")) == 3
    assert _stage(app, base, out["dir"], storage.FINAL).exists()
    assert out["stages"][-1]["source"] == "draft-fallback"


def test_revise_retries_when_model_returns_review_json(app, monkeypatch):
    """C 段偶尔把【审稿报告】当正文返回 → 校验拦下重做，绝不写进定稿。

    实测事故（2026-10-17）：5 篇成品里 2 篇的 03_final.html 装的就是审稿 JSON。
    """
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT], [_review(78, "需要修改")],
                     finals=[_review(78, "需要修改"), FINAL])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert out["degraded"] is False
    assert len(script.steps("C")) == 2, "第一次是 JSON，必须重试"
    final = _stage(app, base, out["dir"], storage.FINAL).read_text(encoding="utf-8")
    assert "overall_score" not in final, "审稿 JSON 绝不能进定稿"
    assert "<h2>" in final


def test_revise_falls_back_to_draft_when_output_never_valid(app, monkeypatch):
    """C 段一直返回 JSON → 不写进定稿，改用初稿降级交付（内容仍可用）。"""
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT], [_review(78, "需要修改")],
                     finals=[_review(78, "需要修改")])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert out["degraded"] is True
    assert out["stages"][-1]["source"] == "draft-fallback"
    final = _stage(app, base, out["dir"], storage.FINAL).read_text(encoding="utf-8")
    assert "overall_score" not in final
    assert "先鉴定再报价" in final


def test_dimension_scores_are_really_weighted():
    """七维要**真加权**：总分由分维度分数按权重算出，覆盖模型自己报的整体分。

    权重：一 30% / 二 15% / 三 20% / 四 10% / 五 5% / 六 8% / 七 12%
    """
    from app.services.writer.review_compat import apply_weights

    def full(dim1: int) -> dict:
        return {"dimension_scores": {
            "维度一：事实与专业准确性": dim1,
            "维度二：逻辑严密性": 100,
            "维度三：结构完整性与GEO可引用性": 100,
            "维度四：专业深度与信息密度": 100,
            "维度五：品牌融入质量": 100,
            "维度六：文字质量与阅读体验": 100,
            "维度七：合规与风险": 100},
            "overall_score": 60, "verdict": "需要修改"}

    perfect = apply_weights(full(100))
    assert perfect["overall_score"] == 100 and perfect["verdict"] == "可直接发布"

    # 维度一只有 40 分 → 40*0.30 + 100*0.70 = 82，且 verdict 跟着重算
    weak_facts = apply_weights(full(40))
    assert weak_facts["overall_score"] == 82
    assert weak_facts["verdict"] == "需要修改", "分数与 verdict 必须同步重算"


def test_final_review_runs_and_reverts_when_worse(app, monkeypatch):
    """C 段改完要复评；复评 <60（说明改坏了）→ 回退用初稿当定稿。"""
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT], [_review(78, "需要修改"), _review(52, "建议重写")])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert out["final_score"] == 52 and out["final_verdict"] == "建议重写"
    assert out.get("final_reverted") is True, "改坏了的定稿不能交付"
    # 回退会**直接替换已落盘的 03_final.html**（静默数据变更），动作必须记账 ——
    # 被拒（已发布/无初稿）时也要能从返回值看出"没回退、需人工复核"。
    assert out["final_revert_action"] == "reverted"
    final = _stage(app, base, out["dir"], storage.FINAL).read_text(encoding="utf-8")
    assert "先鉴定再报价" in final, "应回退成初稿内容"


def test_total_llm_calls_are_bounded(app, monkeypatch):
    """最坏情况也不能失控：token 预算必须封顶（审稿一直判建议重写）。"""
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT], [_review(52, "建议重写")])
    _install(app, monkeypatch, script)

    _run(app, base)
    assert len(script.calls) <= 16, f"单篇调用次数失控：{len(script.calls)}"
    assert len(script.steps("A")) == 3, "重写轮数必须有硬上限"


# ── ④ 重写轮的数据安全（2026-10-17 全项目审查）──────────────────────────────
#
# 重写轮**必须先删掉上一轮的初稿与审稿**（否则 A 段按"初稿存在即跳过"直接返回
# skipped，重写根本不发生）。但删完到新初稿写出来之间，那两份是**唯一副本** ——
# 期间 generate_article 的任何失败（空正文、品牌校验、网络）都会让文章目录变空，
# 文章凭空消失。所以删前必须备份、失败必须回滚。

def test_rewrite_round_keeps_article_when_draft_generation_fails(app, monkeypatch):
    """第二轮初稿生成失败 → 回滚上一轮初稿并降级交付，绝不能把文章弄没。"""
    base = app.config["DATA_ROOT"]
    # 第二个 A 段输出为空 → generate_article 抛 ValidationError
    script = _Script([DRAFT, ""], [_review(52, "建议重写")])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert out["degraded"] is True
    assert len(script.steps("A")) == 2, "第二轮确实尝试过生成（不是断点续跑跳过）"

    dir_name = out["dir"]
    draft = _stage(app, base, dir_name, storage.DRAFT)
    assert draft.exists(), "回滚丢了初稿 —— 文章被重写轮删没了"
    assert "先鉴定再报价" in draft.read_text(encoding="utf-8"), "回滚的应是上一轮初稿"
    assert _stage(app, base, dir_name, storage.FINAL).exists(), "仍需交付一篇可用文章"


def test_revert_refuses_to_overwrite_published_article(app):
    """已发布的稿子不覆盖：磁盘那份可能已经分发出去，换成初稿会让档案与线上永久不一致。

    发布记录在 PublishLog 里，**不会**跟着回退 —— 所以这里必须拒绝，而不是"尽力"。
    """
    from app.extensions import db
    from app.models import Article
    from app.services.writer.recover import _revert_to_draft
    from app.tenancy import TenantScopedRepository, tenant_scope

    base = app.config["DATA_ROOT"]
    with app.app_context(), tenant_scope("t_alpha"):
        storage.write_article_file("已发布样本", storage.DRAFT, "<p>初稿</p>")
        storage.write_article_file("已发布样本", storage.FINAL, "<p>已分发的定稿</p>")
        TenantScopedRepository(Article).add(
            Article(dir="已发布样本", title="已发布样本", status="published"))
        db.session.commit()

        assert _revert_to_draft("已发布样本", base) == "skipped-published"
        assert storage.read_article_file("已发布样本", storage.FINAL) \
            == "<p>已分发的定稿</p>", "已发布的定稿被静默替换了"


def test_revert_returns_status_instead_of_raising_when_draft_missing(app):
    """回退失败只记账、不抛异常：调用点在**成功返回路径**上，外面没有兜底。

    旧实现裸调 `read_article_file`，异常会一路穿透把一次成功的流程变成失败任务。
    """
    from app.services.writer.recover import _revert_to_draft
    from app.tenancy import tenant_scope

    base = app.config["DATA_ROOT"]
    with app.app_context(), tenant_scope("t_alpha"):
        storage.write_article_file("没有初稿的样本", storage.FINAL, "<p>定稿</p>")
        assert _revert_to_draft("没有初稿的样本", base) == "skipped-no-draft"
        assert storage.read_article_file("没有初稿的样本", storage.FINAL) == "<p>定稿</p>"


def test_publishable_verdict_needs_no_rewrite(app, monkeypatch):
    """可直接发布 → 不重写、不调 C 段（既有行为不得被回归环破坏）。"""
    base = app.config["DATA_ROOT"]
    script = _Script([DRAFT], [_review(92, "可直接发布")])
    _install(app, monkeypatch, script)

    out = _run(app, base)
    assert out["rewrites"] == 0 and out["rounds"] == 1
    assert script.steps("C") == [], "可直接发布时不该调 LLM"
    assert out["stages"][-1]["source"] == "draft-reused"
