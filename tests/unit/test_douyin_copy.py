"""抖音图文文案生成的回归测试。

背景（2026-10-16 定位）：`copy_for()`（当时在 douyin_jobs.py，现迁至
services/douyin_pack.py）把 `LLMClient` **当函数直接调用**
（`build_llm_client(cfg)([...])`），而正确用法是 `.complete(messages)`。
于是每次都抛 `'LLMClient' object is not callable` → 永远走兜底；
而兜底又把 `creative_prompt` 模板**原样当成正文**，用户看到的"文案"
其实是一段指令文本。视频产线（video_jobs._script）是同一个 bug。
"""
from __future__ import annotations

import json

from app.services import llm as llm_mod


class _FakeClient:
    """只实现 .complete —— 若被测代码把它当函数调用，会立刻 TypeError。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        return self.reply


def test_douyin_copy_goes_through_llm(app, monkeypatch):
    """正常路径：走 LLMClient.complete，解析 JSON，去掉标签里的 #。"""
    from app.services.douyin_pack import copy_for

    client = _FakeClient('```json\n{"title":"断镯修复怎么选","description":"正文文案内容",'
                         '"tags":["#同城服务","二手镯"]}\n```')
    monkeypatch.setattr(llm_mod, "build_llm_client", lambda cfg: client)
    with app.app_context():
        title, desc, tags, from_llm = copy_for(
            {"copy_prompt": "写文案", "creative_prompt": "主题X"},
            "杭州", "阿尔法珠宝", "13800000000")
    assert from_llm is True, "文案必须真的走过 LLM"
    assert client.calls, "必须调用过 .complete"
    assert title == "断镯修复怎么选"
    assert desc == "正文文案内容"
    assert tags == ["同城服务", "二手镯"], "标签要剥掉 # 前缀"


def test_douyin_copy_accepts_plain_json(app, monkeypatch):
    """LLM 直接吐裸 JSON（无围栏）也要能解析。"""
    from app.services.douyin_pack import copy_for

    client = _FakeClient(json.dumps({"title": "标题", "description": "描述",
                                     "tags": ["a"]}, ensure_ascii=False))
    monkeypatch.setattr(llm_mod, "build_llm_client", lambda cfg: client)
    with app.app_context():
        _, desc, _, from_llm = copy_for(
            {"copy_prompt": "p", "creative_prompt": "t"}, "苏州", "B", "1")
    assert from_llm is True and desc == "描述"


def test_douyin_copy_keeps_long_body(app, monkeypatch):
    """copy_prompt 要求正文 600-900 字：截断上限不能把文案拦腰砍断。"""
    from app.services.douyin_pack import copy_for

    body = "文" * 800
    client = _FakeClient(json.dumps({"title": "t", "description": body, "tags": []},
                                    ensure_ascii=False))
    monkeypatch.setattr(llm_mod, "build_llm_client", lambda cfg: client)
    with app.app_context():
        _, desc, _, _ = copy_for({"copy_prompt": "p"}, "杭州", "B", "1")
    assert len(desc) == 800, "800 字正文不应被截断到 500"


def test_douyin_copy_fallback_never_leaks_prompt(app, monkeypatch):
    """LLM 挂掉时兜底：不能把 prompt 模板当正文，并标记 from_llm=False。"""
    from app.services.douyin_pack import copy_for

    def boom(cfg):
        raise RuntimeError("上游 LLM 挂了")

    monkeypatch.setattr(llm_mod, "build_llm_client", boom)
    with app.app_context():
        title, desc, tags, from_llm = copy_for(
            {"copy_prompt": "【绝密指令】你是抖音编辑",
             "creative_prompt": "【主题模板】必须正好5个标签"},
            "杭州", "阿尔法", "13800000000")
    assert from_llm is False
    assert "绝密指令" not in desc, "兜底不得复述 copy_prompt"
    assert "主题模板" not in desc, "兜底不得复述 creative_prompt"
    assert "杭州" in desc, "兜底要给中性可用文案"
    # 兜底不得带电话：copy_prompt 明确"不写任何电话号码"（旧兜底是"…详询 {phone}"，
    # 会把号码写进每条兜底稿，实测污染了 25 个内容包）
    assert "13800000000" not in desc, "兜底不得把号码带进文案"
    assert tags == ["修复知识", "知识分享"]


def test_video_script_goes_through_llm(app, monkeypatch):
    """视频产线同一个 bug 的回归锁。"""
    from app.blueprints.generation import video_jobs

    client = _FakeClient(json.dumps({"title": "视频标题", "desc": "发布文案",
                                     "tags": ["同城"], "shots": ["分镜1", "分镜2"]},
                                    ensure_ascii=False))
    monkeypatch.setattr(llm_mod, "build_llm_client", lambda cfg: client)
    with app.app_context():
        out = video_jobs._script("阿尔法", "杭州", "断镯修复", "13800000000")
    assert client.calls, "视频分镜也必须走 .complete"
    assert out["title"] == "视频标题" and out["shots"] == ["分镜1", "分镜2"]
