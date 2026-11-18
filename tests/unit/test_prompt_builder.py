"""Prompt 渲染器：变量解析自 Prompt 正文，缺必填变量即报错。"""
from __future__ import annotations

import re

import pytest

from app.errors import ConfigError
from app.services.writer.prompt_builder import PromptLibrary


@pytest.fixture(scope="module")
def lib() -> PromptLibrary:
    return PromptLibrary()


def test_required_vars_parsed_from_prompt_text(lib):
    assert "brand_data" in lib.required("A")
    assert "contact_rule" in lib.required("A")
    assert "article" in lib.required("B")
    assert "review" in lib.required("C")
    assert "genre_mix" in lib.required("T")


def test_html_comments_are_stripped(lib):
    """变量清单/8 视角附录都在 HTML 注释里，不应发给模型。"""
    rendered = lib.render("A", {n: "x" for n in lib.required("A")})
    assert "<!--" not in rendered
    assert "变量清单" not in rendered


def test_render_replaces_every_placeholder(lib):
    for step in ("T", "A", "B", "C"):
        variables = {name: f"__{name}__" for name in lib.required(step)}
        out = lib.render(step, variables)
        leftovers = re.findall(r"\{[a-z][a-z0-9_]*\}", out)
        assert not leftovers, f"{step} 段仍有未替换占位符：{leftovers}"
        assert "__brand_data__" in out if "brand_data" in variables else True


def test_missing_required_variable_raises(lib):
    with pytest.raises(ConfigError):
        lib.render("A", {})


def test_non_strict_allows_missing(lib):
    out = lib.render("A", {}, strict=False)
    assert "{brand_data}" in out


def test_unknown_step_raises(lib):
    with pytest.raises(ConfigError):
        lib.required("Z")
