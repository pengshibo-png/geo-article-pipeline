"""抖音图文「提示词占位符」与「违禁词入 Prompt」的回归测试（2026-10-19）。

页面（三块「变量说明」）承诺了 `{brand} {city} {phone} {address} {brand_data}
{banned} {n} {copy} {service} {price_lines}`，而改造前代码只 replace 过 `{city}`
一处 —— 模型拿到的是字面量花括号（实测文案里出现过"品牌：{brand}"）。
违禁词也一样：以前只在生成**之后**往 qc 里标一句，模型压根不知道哪些词不能写。
"""
from __future__ import annotations

import json

from app import tenancy
from app.services import douyin_vars


def test_render_substitutes_known_vars():
    out = douyin_vars.render("城市：{city}｜品牌：{brand}", {"city": "杭州", "brand": "阿尔法"})
    assert out == "城市：杭州｜品牌：阿尔法"


def test_render_keeps_unknown_vars_literal():
    """拼错的变量**保留原样**（同 A/B/C 段 custom_text 口径）—— 悄悄替换成空串会
    让"指令写错变量名"变成静默失效，用户只会觉得"模型不听话"。"""
    out = douyin_vars.render("{city}{cityy}", {"city": "杭州"})
    assert out == "杭州{cityy}"


def test_default_address_matches_page_preview():
    assert douyin_vars.default_address("杭州", ["杭州", "上海市", "杭州"]) == "杭州·全国2城就近上门"
    assert douyin_vars.default_address("", ["杭州"]) == ""


def test_banned_text_joins_words():
    assert douyin_vars.banned_text({"banned_words": ["最好", "第一", "", "包治"]}) == "最好、第一、包治"


def _fake_client(seen: list, reply: dict):
    class _C:
        def complete(self, messages, **kw):
            seen.append(messages[-1]["content"])
            return json.dumps(reply, ensure_ascii=False)
    return _C()


def test_copy_prompt_renders_vars_and_injects_banned(app, monkeypatch):
    """文案提示词：占位符全部替换 + 品牌资料真的喂进去 + 违禁词进 Prompt。"""
    from app.services import llm as llm_mod
    from app.services.douyin_pack import copy_for

    seen: list = []
    monkeypatch.setattr(llm_mod, "build_llm_client",
                        lambda cfg: _fake_client(seen, {"title": "标题", "description": "正文",
                                                        "tags": ["a"]}))
    cfg = {"copy_prompt": "为{brand}在{city}写稿，服务行：{address}，禁用{phone}",
           "banned_words": ["绝对", "最便宜"],
           "cities": ["杭州", "上海市"]}      # 空 address → 按推广城市自动生成
    with app.app_context(), tenancy.tenant_scope("t_alpha"):
        title, _desc, _tags, from_llm = copy_for(cfg, "杭州", "阿尔法珠宝", "13800000000")

    prompt = seen[0]
    assert from_llm and title == "标题"
    assert "{brand}" not in prompt and "{city}" not in prompt and "{address}" not in prompt
    assert "为阿尔法珠宝在杭州写稿" in prompt
    assert "服务行：杭州·全国2城就近上门" in prompt, "{address} 空值要走按城市自动生成"
    assert "【禁用词（一律不得出现，出现即视为不合格）】绝对、最便宜" in prompt, \
        "违禁词必须进 Prompt，不能只在事后 qc 里标注"
    assert "阿尔法珠宝" in prompt and "断镯修复" in prompt, "品牌资料（事实来源）要被喂进去"


def test_copy_prompt_carries_brand_facts(app, monkeypatch):
    """`{brand_data}` 必须是品牌资料原文（模型只能依据这里的事实）。"""
    from app.services import llm as llm_mod
    from app.services.douyin_pack import copy_for

    seen: list = []
    monkeypatch.setattr(llm_mod, "build_llm_client",
                        lambda cfg: _fake_client(seen, {"title": "t", "description": "d",
                                                        "tags": []}))
    with app.app_context(), tenancy.tenant_scope("t_alpha"):
        copy_for({"copy_prompt": "资料：{brand_data}"}, "杭州", "阿尔法珠宝", "")
    assert "91330100MA0000000X" in seen[0], "统一社会信用代码来自种子品牌资料"


def test_slide_plan_reads_creative_prompt_and_banned(app, monkeypatch):
    """创意一体化指令真的驱动配图规划（`{n}`/`{copy}` 替换），违禁词同样入图。"""
    from app.services import llm as llm_mod
    from app.services.douyin_visual import plan_pack

    seen: list = []
    monkeypatch.setattr(llm_mod, "build_llm_client",
                        lambda cfg: _fake_client(seen, {"slides": [
                            {"role": "cover", "headline": "标题"}], "tags": ["同城"]}))
    cfg = {"creative_prompt": "给{brand}的{city}服务做{n}张图，文案：{copy}",
           "banned_words": ["绝对"]}
    with app.app_context(), tenancy.tenant_scope("t_alpha"):
        plan = plan_pack(cfg, "这是一段文案", "杭州", 3)

    prompt = seen[0]
    assert "{n}" not in prompt and "{copy}" not in prompt
    assert "3张图" in prompt and "这是一段文案" in prompt
    assert "【禁用词（图中的文字一律不得出现）】绝对" in prompt
    assert "3 张" in prompt, "张数要写进任务要求"
    assert plan["tags"] == ["同城"], "标签由创意指令摘出来"


def test_image_model_reaches_provider(app, monkeypatch):
    """`image_model` 是活配置：页面填的模型要真的走到生图请求体里。

    改造前它是**死配置** —— 页面存得下、上游却始终用系统设置里的模型，
    用户换了模型界面上"保存成功"，出图却一点变化没有。
    """
    from app.services import imagegen

    sent: dict = {}

    def fake_post(url, payload, key):  # noqa: ANN001
        sent["url"] = url
        sent["payload"] = json.loads(payload.decode("utf-8"))
        return {"data": [{"b64_json": "AA=="}]}

    monkeypatch.setattr(imagegen, "conf", lambda: {
        "key": "k", "base_url": "https://api.example.com/v1",
        "protocol": "openai-images", "model": "system-default"})
    monkeypatch.setattr(imagegen, "_post_images", fake_post)

    with app.app_context():
        imagegen.generate("提示词", size="1024x1024", provider="openai-images",
                          model="my-model")
    assert sent["payload"]["model"] == "my-model", "页面配置的模型要覆盖系统默认"
    assert sent["payload"]["size"] == "1024x1024"

    sent.clear()
    with app.app_context():
        imagegen.generate("提示词", provider="openai-images")   # 不传 → 回落到系统模型
    assert sent["payload"]["model"] == "system-default"