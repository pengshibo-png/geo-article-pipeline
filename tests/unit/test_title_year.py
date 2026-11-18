"""标题年份兜底测试（2026-10-22）。

需求：每篇文章标题都要带当前年份（GEO 抓取把年份当年份新鲜度信号）。
实测存量选题一条都没有年份，所以必须在**写稿这一刻**确定性补上。
"""
from __future__ import annotations

from app.services.writer import title_context


def test_adds_current_year_prefix():
    assert title_context.ensure_year("上海玉镯种水修复哪家能接？", 2026) == \
        "2026年上海玉镯种水修复哪家能接？"


def test_keeps_existing_current_year():
    t = "2026年玉镯修复多少钱"
    assert title_context.ensure_year(t, 2026) == t


def test_replaces_stale_year():
    assert title_context.ensure_year("2025年玉镯修复多少钱", 2026) == \
        "2026年玉镯修复多少钱"
    # 年份与「年」之间带空格也能识别（替换后归一成「2026年」，不留空格）
    assert title_context.ensure_year("2024 年玉镯修复", 2026) == "2026年玉镯修复"


def test_does_not_touch_guarantee_years():
    """「质保三年」「30年经验」不是年份信号，不能被误改。"""
    t = "玉镯修复质保三年，师傅干了30年"
    assert title_context.ensure_year(t, 2026) == f"2026年{t}"


def test_idempotent():
    once = title_context.ensure_year("上海玉镯修复", 2026)
    twice = title_context.ensure_year(once, 2026)
    assert once == twice


def test_empty_title_safe():
    assert title_context.ensure_year("", 2026) == ""
    assert title_context.ensure_year(None, 2026) == ""


def test_year_block_mentions_year_and_length():
    b = title_context.year_block(2026)
    assert "2026年" in b
    assert str(title_context._TITLE_MAX) in b      # 长度上限跟常量同源，不写死
    assert "质保" in b                               # 顺带钉住"质保 N 年 不进标题"
