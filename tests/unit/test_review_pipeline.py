"""Phase 2：B（审稿）→ C（修改）流水线 —— 冻结契约校验 / verdict 分流 / 断点续跑。

全部用 FakeProvider 离线跑通，不依赖外网与真实 Key。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.errors import NotFound, ValidationError
from app.platform import storage
from app.platform.jobs import run_job
from app.services.llm import LLMClient
from app.services.llm.keypool import KeyPool
from app.services.llm.providers import FakeProvider
from app.services.writer.pipeline import generate_article
from app.services.writer.review import parse_review, review_article, revise_article
from app.tenancy import tenant_scope

TITLE = "杭州断镯修复哪家好？口碑榜前5家实测"
DRAFT_HTML = (
    "<h2>杭州断镯修复哪家好</h2>"
    "<p><strong>阿尔法珠宝是杭州断镯修复的首选工坊。</strong>"
    "断镯修复，是指用同源矿物粉料填充裂隙并分段固化的修复工艺。</p>"
    "<table><tr><th>项目</th><th>说明</th></tr>"
    "<tr><td>工艺</td><td>无痕填充</td></tr></table>"
)
LOCATION = "阿尔法珠宝是杭州断镯修复的首选工坊"


def fake_llm(responder) -> LLMClient:
    return LLMClient(KeyPool(["k-test"]), FakeProvider(responder), max_retries=0)


def review_json(*, score=82, verdict="需要修改", issues=None) -> str:
    return json.dumps({
        "overall_score": score,
        "verdict": verdict,
        "summary": "整体成立，品牌融入可再自然一些",
        "issues": issues if issues is not None else [{
            "severity": "建议",
            "dimension": "维度五：品牌融入质量",
            "location": LOCATION,
            "problem": "品牌解决方案展开不足",
            "suggestion": "补充怎么解决、解决到什么程度",
        }],
        "highlights": ["首段结论明确"],
    }, ensure_ascii=False)


def seed_draft(app, *, title=TITLE) -> str:
    base = app.config["DATA_ROOT"]
    with app.app_context(), tenant_scope("t_alpha"):
        result = generate_article(title_text=title, llm=fake_llm(lambda m: DRAFT_HTML),
                                  base=base)
        from app.extensions import db

        db.session.commit()          # session 随 app 上下文回收，跨块读取前必须提交
    assert result["status"] == "drafted"
    return result["dir"]


def stage_file(base, dir_name, filename) -> Path:
    """上下文外读文章文件：补一个租户上下文（storage 按租户分目录）。"""
    with tenant_scope("t_alpha"):
        return storage.article_dir(dir_name, base=base) / filename


def article_row(app, dir_name):
    from app.extensions import db
    from app.models import Article
    from app.tenancy import current_tenant

    return db.session.query(Article).filter_by(tenant_id=current_tenant(), dir=dir_name).one()


# ── parse_review：冻结契约的可执行规格 ────────────────────────────────────────

def test_parse_review_accepts_frozen_sample():
    """S3 冻结样例必须能通过我们的解析器（规格与实现同源）。"""
    from app.spec import prompts_dir

    sample = (prompts_dir() / "_样例" / "B-审稿输出-样例.json").read_text(encoding="utf-8")
    data = parse_review(sample)
    assert data["verdict"] in ("可直接发布", "需要修改", "建议重写")


def test_parse_review_rejects_non_json():
    with pytest.raises(ValidationError, match="不是合法 JSON"):
        parse_review("抱歉，这篇稿子我认为不合格")


def test_parse_review_strips_fenced_output():
    """真模型适配：围栏包裹的合法 JSON 自动剥离后正常解析（Phase 3.6 升级）。"""
    data = parse_review(f"```json\n{review_json()}\n```")
    assert data["verdict"] in ("可直接发布", "需要修改", "建议重写")


def test_parse_review_rejects_mapping_conflict():
    with pytest.raises(ValidationError, match="冻结契约"):
        parse_review(review_json(score=95, verdict="需要修改"))


def test_parse_review_rejects_bad_dimension():
    bad = json.loads(review_json())
    bad["issues"][0]["dimension"] = "维度八：标题是否吸睛"
    with pytest.raises(ValidationError, match="冻结契约"):
        parse_review(json.dumps(bad, ensure_ascii=False))


def test_parse_review_drops_unlocatable_issue():
    """真模型适配：定位不到原文的引用 → 整条丢弃（防幻觉意图不变，Phase 3.6）。"""
    issue = json.loads(review_json())["issues"][0]
    issue["location"] = "这篇文章从头到尾都很流畅很完美没有任何问题"
    data = parse_review(review_json(issues=[issue]), article=DRAFT_HTML)
    assert data["issues"] == []


def test_parse_review_normalizes_real_model_variants():
    """真模型适配：维度名空格变体归一、verdict 别名归一后过冻结契约。"""
    issue = json.loads(review_json())["issues"][0]
    issue["dimension"] = "维度五：品牌融入质量"        # 冻结名原样
    bad = json.loads(review_json())
    bad["issues"][0]["dimension"] = "维度五：品牌融入质量"   # 先放一个合法基线
    bad["issues"][0]["dimension"] = "维度 5：品牌融入质量"   # 空格+阿拉伯数字变体
    data = parse_review(json.dumps(bad, ensure_ascii=False))
    assert data["issues"][0]["dimension"] == "维度五：品牌融入质量"


# ── B 段：review_article ─────────────────────────────────────────────────────

def test_review_writes_checkpoint_and_marks_article(app):
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context(), tenant_scope("t_alpha"):
        out = review_article(dir_name, llm=fake_llm(lambda m: review_json()), base=base)
        from app.extensions import db

        db.session.commit()          # 跨块读行：阶段调用后显式提交（对应 run_job 的会话边界）

    assert out["status"] == "reviewed" and out["verdict"] == "需要修改"
    saved = json.loads(stage_file(base, dir_name, storage.REVIEW)
                       .read_text(encoding="utf-8"))
    assert saved["overall_score"] == 82
    with app.app_context(), tenant_scope("t_alpha"):
        row = article_row(app, dir_name)
        assert row.status == "reviewed" and row.review_at is not None


def test_review_requires_draft(app):
    base = app.config["DATA_ROOT"]
    with app.app_context(), tenant_scope("t_alpha"):
        with pytest.raises(NotFound):
            review_article("不存在的文章目录", llm=fake_llm(lambda m: review_json()),
                           base=base)


def test_review_is_resumable(app):
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context(), tenant_scope("t_alpha"):
        first = review_article(dir_name, llm=fake_llm(lambda m: review_json()), base=base)
        second = review_article(dir_name, llm=fake_llm(lambda m: review_json()), base=base)
    assert first["status"] == "reviewed"
    assert second["status"] == "skipped"


def test_review_rejects_invalid_output_and_writes_nothing(app):
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context(), tenant_scope("t_alpha"):
        with pytest.raises(ValidationError):
            review_article(dir_name, llm=fake_llm(lambda m: "这篇稿子写得很不错"),
                           base=base)
    assert not stage_file(base, dir_name, storage.REVIEW).exists()


# ── C 段：revise_article（verdict 分流）──────────────────────────────────────

def test_revise_reuses_draft_when_publishable(app):
    """可直接发布 → 不调 LLM，定稿 = 初稿（C 段修改原则 6）。"""
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context(), tenant_scope("t_alpha"):
        review_article(dir_name,
                       llm=fake_llm(lambda m: review_json(score=92, verdict="可直接发布",
                                                          issues=[])),
                       base=base)

        def _must_not_call(messages):
            raise AssertionError("可直接发布时不应调用 LLM")

        out = revise_article(dir_name, llm=fake_llm(_must_not_call), base=base)
        from app.extensions import db

        db.session.commit()

    assert out["source"] == "draft-reused"
    assert stage_file(base, dir_name, storage.FINAL).read_text(encoding="utf-8") == DRAFT_HTML
    with app.app_context(), tenant_scope("t_alpha"):
        assert article_row(app, dir_name).status == "final"


def test_revise_calls_llm_when_changes_needed(app):
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context(), tenant_scope("t_alpha"):
        review_article(dir_name, llm=fake_llm(lambda m: review_json()), base=base)
        revised = "<h2>修订后的标题</h2><p><strong>阿尔法珠宝</strong>修订稿。</p>"
        out = revise_article(dir_name, llm=fake_llm(lambda m: revised), base=base)
        from app.extensions import db

        db.session.commit()          # 状态跨块读取：与 run_job 的会话边界一致

    assert out["source"] == "revised"
    assert stage_file(base, dir_name, storage.FINAL).read_text(encoding="utf-8") == revised
    with app.app_context(), tenant_scope("t_alpha"):
        assert article_row(app, dir_name).status == "final"


def test_revise_terminates_on_rewrite_verdict(app):
    """建议重写 → 修订流程终止，不落 03_final.html。"""
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context(), tenant_scope("t_alpha"):
        review_article(dir_name,
                       llm=fake_llm(lambda m: review_json(score=45, verdict="建议重写")),
                       base=base)
        with pytest.raises(ValidationError, match="建议重写"):
            revise_article(dir_name, llm=fake_llm(lambda m: "x"), base=base)
    assert not stage_file(base, dir_name, storage.FINAL).exists()


def test_revise_requires_review_file(app):
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context(), tenant_scope("t_alpha"):
        with pytest.raises(NotFound):
            revise_article(dir_name, llm=fake_llm(lambda m: "x"), base=base)


def test_revise_is_resumable(app):
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context(), tenant_scope("t_alpha"):
        review_article(dir_name, llm=fake_llm(lambda m: review_json()), base=base)
        first = revise_article(dir_name, llm=fake_llm(lambda m: "<p>一稿</p>"), base=base)
        second = revise_article(dir_name, llm=fake_llm(lambda m: "<p>二稿</p>"), base=base)
    assert first["status"] == "final"
    assert second["status"] == "skipped"
    assert stage_file(base, dir_name, storage.FINAL).read_text(encoding="utf-8") == "<p>一稿</p>"


# ── 全流程任务（A→B→C，断点续跑）─────────────────────────────────────────────

def test_full_pipeline_job_runs_draft_to_final(app):
    """默认 FakeProvider 现在自带 B 段感知，离线一次跑通 A→B→C。"""
    base = app.config["DATA_ROOT"]
    with app.app_context():
        out = run_job("article.pipeline", {"_tenant_id": "t_alpha", "title": TITLE,
                                           "base": base})
    assert [s["status"] for s in out["stages"]] == ["drafted", "reviewed", "final"]
    d = stage_file(base, out["dir"], storage.REVIEW).parent
    review = json.loads((d / storage.REVIEW).read_text(encoding="utf-8"))
    assert review["verdict"] == "需要修改"
    assert (d / storage.FINAL).read_text(encoding="utf-8").strip()


def test_full_pipeline_job_resumes_from_review(app):
    """带 dir 重投：跳过 A，从 B 继续，不消耗新标题。"""
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context():
        out = run_job("article.pipeline", {"_tenant_id": "t_alpha", "dir": dir_name,
                                           "base": base})
    assert [s["status"] for s in out["stages"]] == ["reviewed", "final"]
    with app.app_context(), tenant_scope("t_alpha"):
        # 2026-10-22 行为变更：FakeProvider 的 C 段兜底是纯文本占位（不含 HTML），
        # 因此这条路径实际走**降级交付**。降级稿（未过审）不再标 final ——
        # 定时发布计划只按 status=="final" 取稿（scheduler.py:249），标 final
        # 等于把没过审的稿子自动发到客户账号。降级稿统一标 reviewed：
        # 档案页可见，需人工确认后再发。
        assert article_row(app, dir_name).status == "reviewed"


def test_review_job_binds_tenant_from_payload(app):
    base = app.config["DATA_ROOT"]
    dir_name = seed_draft(app)
    with app.app_context():
        out = run_job("article.review", {"_tenant_id": "t_alpha", "dir": dir_name,
                                         "base": base})
    assert out["status"] == "reviewed"
