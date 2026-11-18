"""标题压缩测试（2026-10-22）。

平台把「标题过长 / 不完整」判低质，但过度压缩会丢掉用户真正在搜的信息
（实测把「手镯崩口缺肉要补配，矿物粉料配色，报价300元起」压成「…要补配」，
价格这个搜索核心没了）。这里锁住压缩策略的优先级与边界。
"""
from __future__ import annotations

from app.services.writer.title_context import _TITLE_MAX, normalize_title, shortify


def test_under_limit_untouched():
    t = "玉镯断了两截怎么修"
    assert shortify(t) == t


def test_question_cut_keeps_complete_sentence():
    t = "上海聚贤阁轻度惊纹的玉镯无痕修复价格差很大怎么比？透明报价与手艺对照"
    out = shortify(t, 25)
    assert out == "上海聚贤阁轻度惊纹的玉镯无痕修复价格差很大怎么比？"
    assert out.endswith("？")


def test_question_then_append_clauses_until_limit():
    """问号后还有余量时要继续补，不要只留问句。"""
    t = "上海维修上门收费怎么算？书面报价加资质核验，别急着付定金"
    out = shortify(t, 30)
    assert out.startswith("上海维修上门收费怎么算？")
    assert "书面报价加资质核验" in out
    assert len(out) <= 30


def test_comma_clauses_accumulate_not_first_only():
    """逗号结构要逐段累加 —— 只取第一段会丢掉「报价300元起」。"""
    t = "手镯崩口缺肉要补配，矿物粉料配色，报价300元起先拍图定损"
    out = shortify(t, 29)
    assert "报价300元起" in out
    assert len(out) <= 29


def test_no_ellipsis_ending():
    """平台把「……结尾」判成标题不完整，压缩不许用省略号。"""
    out = shortify("上海" * 30, 20)
    assert not out.endswith(("…", "..."))


def test_normalize_title_year_and_length():
    """**2026-11-03 行为变更**：上限从 35 收紧到 30。

    用户实测「30 字以上的标题在很多号上发不出去」（平台 schema 拒、整批失败），
    所以宁可丢副标题也要保证能发。这条标题压到 30 后确实保不住「报价300元起」——
    这是**有意的取舍**，下面只锁「带年份 + 不超上限 + 按标点边界截断」。
    """
    t = "手镯崩口缺肉要补配，矿物粉料配色，报价300元起先拍图定损"
    out = normalize_title(t, year=2026)
    assert out.startswith("2026年")
    assert len(out) <= _TITLE_MAX
    assert not out.endswith(("…", "..."))       # 不能用省略号收尾
    assert out.endswith(("配", "色")) or len(out) <= _TITLE_MAX


def test_normalize_title_idempotent():
    t = "2026年玉镯修复多少钱"
    assert normalize_title(t, year=2026) == t
