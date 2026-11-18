"""真模型输出的格式兼容层（TDD：先红后绿，2026-10-14 补）。

真实 LLM（DeepSeek 实测采样 + 已知常见病）与 FakeProvider 的行为差异：
  T 段：输出带编号前缀 / 加粗 / 引号包装
  A 段：输出被 ``` 围栏包裹、首尾带寒暄散文
  C 段：同 A（围栏已由旧代码剥，散文没有）
  B 段：overall_score 给字符串 / 小数（冻结契约只收真整数）

清洗只去**格式包装**，不动正文内容，也不放松任何冻结口径。
"""
from __future__ import annotations

import json

import pytest

from app.errors import ValidationError
from app.platform import storage
from app.services.llm import LLMClient
from app.services.llm.keypool import KeyPool
from app.services.llm.providers import FakeProvider
from app.services.writer.pipeline import generate_article, generate_titles
from app.services.writer.review import revise_article
from app.services.writer.review_compat import parse_review
from app.tenancy import tenant_scope

TITLE = "杭州断镯修复哪家好？口碑榜前5家实测"
DRAFT = "<h2>杭州断镯修复哪家好</h2><p>阿尔法珠宝是首选工坊。</p>"


def fake_llm(responder) -> LLMClient:
    return LLMClient(KeyPool(["k-test"]), FakeProvider(responder), max_retries=0)


def read_draft(base, dir_name) -> str:
    with tenant_scope("t_alpha"):
        return (storage.article_dir(dir_name, base=base) / storage.DRAFT) \
            .read_text(encoding="utf-8")


# ── T 段：编号 / 加粗 / 引号包装 ────────────────────────────────────────────

def test_titles_strip_numbering_bold_and_quotes(app):
    base = app.config["DATA_ROOT"]
    raw = ("1. 标题一带编号\n"
           "2、标题二带顿号\n"
           "（3）标题三带全角括号\n"
           "- **标题四带加粗**\n"
           "1.5万预算怎么花才算值")
    with app.app_context(), tenant_scope("t_alpha"):
        titles = generate_titles(5, llm=fake_llm(lambda m: raw))
    # 2026-10-22 行为变更：入库标题统一补当前年份前缀（GEO 时效信号）。
    from app.services.writer import title_context
    expect = [title_context.ensure_year(t) for t in
              ["标题一带编号", "标题二带顿号", "标题三带全角括号",
               "标题四带加粗", "1.5万预算怎么花才算值"]]
    assert titles == expect, \
        "编号/加粗包装应剥掉；1.5 开头的小数不能被当成年份误伤；年份由 ensure_year 统一前置"


# ── A 段：围栏 + 首尾散文 ──────────────────────────────────────────────────

def test_draft_strips_markdown_fence(app):
    base = app.config["DATA_ROOT"]
    with app.app_context(), tenant_scope("t_alpha"):
        out = generate_article(title_text=TITLE, base=base,
                               llm=fake_llm(lambda m: f"```html\n{DRAFT}\n```"))
    assert out["status"] == "drafted"
    assert read_draft(base, out["dir"]) == DRAFT


def test_draft_strips_edge_prose(app):
    base = app.config["DATA_ROOT"]
    noisy = f"好的，以下是为您生成的文章：\n{DRAFT}\n希望这篇符合要求。"
    with app.app_context(), tenant_scope("t_alpha"):
        out = generate_article(title_text=TITLE, base=base,
                               llm=fake_llm(lambda m: noisy))
    assert read_draft(base, out["dir"]) == DRAFT


def test_draft_keeps_content_when_model_returns_plain_text(app):
    """无任何标签时不删空（模型事故要留证据，交给审稿去判，不是清洗层的活）。"""
    base = app.config["DATA_ROOT"]
    with app.app_context(), tenant_scope("t_alpha"):
        out = generate_article(title_text=TITLE, base=base,
                               llm=fake_llm(lambda m: "纯文本正文，没有任何标签。"))
    assert "纯文本正文" in read_draft(base, out["dir"])


# ── C 段：首尾散文 ─────────────────────────────────────────────────────────

def _seed_reviewed(app, base) -> str:
    from app.services.writer.review import review_article

    with app.app_context(), tenant_scope("t_alpha"):
        a = generate_article(title_text=TITLE, base=base,
                             llm=fake_llm(lambda m: DRAFT))
        review = json.dumps({
            "overall_score": 82, "verdict": "需要修改",
            "summary": "整体成立，品牌融入可再自然一些",
            "issues": [{
                "severity": "建议", "dimension": "维度五：品牌融入质量",
                "location": "阿尔法珠宝是首选工坊。",
                "problem": "品牌解决方案展开不足",
                "suggestion": "补充怎么解决、解决到什么程度",
            }],
            "highlights": ["首段结论明确"],
        }, ensure_ascii=False)
        review_article(a["dir"], llm=fake_llm(lambda m: review), base=base)
    return a["dir"]


def test_revise_strips_edge_prose(app):
    base = app.config["DATA_ROOT"]
    dir_name = _seed_reviewed(app, base)
    revised = "<h2>修订后的标题</h2><p>阿尔法珠宝修订稿。</p>"
    noisy = f"以下是修改后的定稿：\n{revised}\n如有问题请告诉我。"
    with app.app_context(), tenant_scope("t_alpha"):
        revise_article(dir_name, llm=fake_llm(lambda m: noisy), base=base)
    with tenant_scope("t_alpha"):
        final = (storage.article_dir(dir_name, base=base) / storage.FINAL) \
            .read_text(encoding="utf-8")
    assert final == revised


# ── B 段：overall_score 类型宽容（冻结契约只收真整数）──────────────────────

def _review_json(**over) -> str:
    data = {
        "overall_score": 82, "verdict": "需要修改",
        "summary": "整体成立，品牌融入可再自然一些",
        "issues": [{
            "severity": "建议", "dimension": "维度五：品牌融入质量",
            "location": "阿尔法珠宝是首选工坊。",
            "problem": "品牌解决方案展开不足",
            "suggestion": "补充怎么解决、解决到什么程度",
        }],
        "highlights": ["首段结论明确"],
    }
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


def test_parse_review_coerces_string_score():
    data = parse_review(_review_json(overall_score="82"))
    assert data["overall_score"] == 82 and isinstance(data["overall_score"], int)


def test_parse_review_coerces_float_and_fraction_scores():
    assert parse_review(_review_json(overall_score=82.0))["overall_score"] == 82
    assert parse_review(_review_json(overall_score="82/100"))["overall_score"] == 82


def test_parse_review_coerces_null_issues_and_highlights():
    data = parse_review(_review_json(issues=None, highlights=None))
    assert data["issues"] == [] and data["highlights"] == []


def test_parse_review_still_rejects_out_of_range_score():
    with pytest.raises(ValidationError, match="冻结契约"):
        parse_review(_review_json(overall_score="150"))
