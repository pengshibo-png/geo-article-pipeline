"""配图规划的回归测试（2026-10-19）。

改造前：4 条固定提示词按 `k % len` 轮转 → 图与文案无关（文案讲质保、图上是
"避坑清单卡"），且条数一多就重复。现在读刚写好的文案来规划这一套图。
"""
from __future__ import annotations

import json

from app.services.douyin_visual import (
    LAYOUTS,
    image_prompts_for,
    plan_slides,
    render_prompt,
)

COPY = (
    "玉镯断了还能不能戴？先说结论：断面干净、能对位的情况下可以拼接加固。"
    "很多人只看修完好不好看，却不问质保保多久。聚贤阁对修复作品提供三年质保，"
    "范围、责任、处理方式都会在送修时讲清。寄修客户全程录像留痕，一单一档存档十二个月以上。"
    "质保之外还要看修复前的判断：先鉴定后修复，区分原生纹理与后天裂纹。"
)


def _fake_llm(app, monkeypatch, payload):
    from app.services.llm import LLMClient
    from app.services.llm.keypool import KeyPool
    from app.services.llm.providers import FakeProvider

    def responder(messages):
        prompt = messages[-1]["content"]
        assert "本篇图文文案全文" in prompt, "图纲必须读的是刚写好的文案"
        return json.dumps(payload, ensure_ascii=False)

    client = LLMClient(KeyPool(["k"]), FakeProvider(responder), max_retries=0)
    monkeypatch.setattr("app.services.llm.build_llm_client", lambda cfg: client)


def test_plan_slides_reads_the_copy(app, monkeypatch):
    """图纲来自文案：标题与要点必须是文案里的东西。"""
    _fake_llm(app, monkeypatch, {"slides": [
        {"role": "cover", "headline": "玉镯断了还能不能戴？",
         "cards": [{"k": "1", "t": "断面能不能对位", "d": "干净能对位才可拼"},
                   {"k": "2", "t": "质保保多久", "d": "三年，覆盖范围写清"}]},
        {"role": "checklist", "headline": "质保到底该看什么",
         "sections": [{"t": "第一看：期限是否明确", "d": "口头不算数",
                       "chips": ["范围", "责任", "处理方式"]}]},
    ]})
    with app.app_context():
        slides = plan_slides({}, COPY, "上海", 2)
    assert len(slides) == 2
    assert slides[0]["role"] == "cover"
    assert "断面" in json.dumps(slides, ensure_ascii=False)
    assert slides[1]["sections"][0]["chips"] == ["范围", "责任", "处理方式"]


def test_render_prompt_carries_style_and_content():
    """渲染出的提示词必须同时含：科普风格底座 + 版式 + 本张内容 + 底部固定行。"""
    prompt = render_prompt({
        "role": "cover", "headline": "修完之后出问题怎么办",
        "cards": [{"k": "1", "t": "质保到底保多久", "d": "修复部位日后会不会发黄"}],
        "contrast": {"left": "送修前只关心好不好看", "right": "聚贤阁提醒你问质保"},
        "tail": "今天把质保讲清楚",
    })
    assert "9:16" in prompt and "科普信息图" in prompt
    assert "#C9A227" in prompt, "金色描边是这套风格的标志"
    assert "修完之后出问题怎么办" in prompt
    assert "质保到底保多久" in prompt and "会不会发黄" in prompt
    assert "对比块" in prompt and "聚贤阁提醒你问质保" in prompt
    # 底部服务行是**通用口径**：原来硬编码『全国连锁门店就近承接』，那是原租户
    # （玉石修复）自己的门店事实 —— 换到别的租户就是把别人家的资质印在自家海报上。
    # 真实口径由产线配置的「服务行内容」({address}) 驱动。
    assert "本地实体门店就近承接" in prompt
    assert "不得出现价格、电话" in prompt


def test_render_prompt_never_invents_brand():
    """品牌名只能来自调用方（当前租户）—— 缺省时**不写这一行**，不编造也不硬编码。"""
    slide = {"role": "cover", "headline": "标题"}
    assert "【顶部正中品牌名" not in render_prompt(slide), "没给品牌就不该凭空造一个"
    p = render_prompt(slide, brand="阿尔法珠宝")
    assert "【顶部正中品牌名" in p and "阿尔法珠宝" in p
    assert "字号加大" in p, "品牌名要加大、醒目"


def test_footer_follows_configured_address(app, monkeypatch):
    """产线配置的「服务行内容」要同时用在文案末行和**海报底部行**（页面承诺）。"""
    from app.services.llm import LLMClient
    from app.services.llm.keypool import KeyPool
    from app.services.llm.providers import FakeProvider

    def boom(messages):
        raise RuntimeError("规划失败 → 走兜底池")

    client = LLMClient(KeyPool(["k"]), FakeProvider(boom), max_retries=0)
    monkeypatch.setattr("app.services.llm.build_llm_client", lambda cfg: client)

    cfg = {"address": "杭州 · 到店与寄修均可", "image_prompts": ["{city}损伤特写"]}
    with app.app_context():
        prompts = image_prompts_for(cfg, COPY, "杭州", 1)
    assert "底部一行小字：杭州 · 到店与寄修均可" in prompts[0]


def test_fallback_without_pool_uses_copy_and_style(app, monkeypatch):
    """图纲规划失败 → **不再有提示词兜底池**，用文案首句 + 画风现拼 N 条。

    2026-11-10 用户要求下线 `image_prompts` 兜底池（它只在规划失败时生效，
    改了看不到效果）。兜底改用文案首句 + 版式轮转，保证仍有 N 张图且不重复。
    """
    from app.services.llm import LLMClient
    from app.services.llm.keypool import KeyPool
    from app.services.llm.providers import FakeProvider

    def boom(messages):
        raise RuntimeError("LLM 不可用")

    client = LLMClient(KeyPool(["k"]), FakeProvider(boom), max_retries=0)
    monkeypatch.setattr("app.services.llm.build_llm_client", lambda cfg: client)

    cfg = {"style_prompt": "竖版 9:16 赛博朋克霓虹科技风，深空蓝黑底"}
    with app.app_context():
        prompts = image_prompts_for(cfg, COPY, "苏州", 4)
    assert len(prompts) == 4
    assert all("赛博朋克霓虹科技风" in p for p in prompts), "画风要进每一条提示词"
    assert all("科普信息图" not in p for p in prompts), "填了画风就不该再套内置米白风"
    # 版式轮转：4 张不能长得一模一样
    assert len(set(prompts)) == 4


def test_four_layouts_are_defined():
    """四种版式齐全（对应样例的封面/知识/清单/流程）。"""
    assert set(LAYOUTS) == {"cover", "knowledge", "checklist", "flow"}
    assert all(LAYOUTS.values())