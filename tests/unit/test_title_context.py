"""「关键词管理」标题品牌化的回归测试（2026-10-17）。

事故：用户反馈 /titles 生成的标题与品牌完全无关。
实测两条根因：
1. `prompts/T-标题选题.md` 的 12 个变量里**没有品牌资料**（正文却写着"品牌名最多
   1 条"，而"下方允许的 1 条"根本没给）；
2. `generate_titles` 只读 `TitleConfig.business`，它为空时兜底成硬编码的
   **「本地服务」** → 玉石修复品牌被生成出"本地服务哪家靠谱""上门安装怎么选"。
"""
from __future__ import annotations

from app.services.llm import LLMClient
from app.services.llm.keypool import KeyPool
from app.services.llm.providers import FakeProvider
from app.services.writer import title_context
from app.services.writer.pipeline import generate_titles
from app.tenancy import tenant_scope

BRAND_MAIN = "阿尔法珠宝"


def _capturing(prompts: list[str]):
    def responder(messages):
        prompts.append(messages[-1]["content"])
        return "杭州断镯修复哪家好？五家实测\n玉镯裂了怎么修？先看这份工艺对照"

    return LLMClient(KeyPool(["k-test"]), FakeProvider(responder), max_retries=0)


def test_titles_prompt_carries_brand_knowledge_base(app):
    """T 段 Prompt 必须带上品牌库与品牌名规则，否则标题与品牌无关。"""
    prompts: list[str] = []
    with app.app_context(), tenant_scope("t_alpha"):
        generate_titles(2, llm=_capturing(prompts))

    assert prompts, "必须调用过 LLM"
    prompt = prompts[0]
    assert "【品牌知识库" in prompt, "品牌知识库块没注入"
    assert BRAND_MAIN in prompt, "品牌主名没进 Prompt"
    assert "【品牌名使用规则】" in prompt, "规格要求「品牌名最多 1 条」，规则必须真正给到"
    assert "【目标受众" in prompt or "受众" not in prompt, "有受众资料时应注入"


def test_titles_never_use_generic_local_service(app):
    """业务定位不能落到硬编码的「本地服务」。"""
    prompts: list[str] = []
    with app.app_context(), tenant_scope("t_alpha"):
        generate_titles(2, llm=_capturing(prompts))

    assert '为"本地服务"业务生成' not in prompts[0]


def test_titles_fall_back_to_brand_name_when_config_empty(app):
    """选题配置没填业务定位时 → 用品牌主名，而不是「本地服务」。

    这正对应用户库里的真实状态（TitleConfig 的 expert/business/directions 全空），
    也是"标题与品牌无关"的直接成因。
    """
    from app.extensions import db
    from app.models import TitleConfig
    from app.tenancy import TenantScopedRepository

    prompts: list[str] = []
    with app.app_context(), tenant_scope("t_alpha"):
        cfg = TenantScopedRepository(TitleConfig).first()
        cfg.business = ""
        db.session.commit()
        generate_titles(2, llm=_capturing(prompts))

    prompt = prompts[0]
    assert f'为"{BRAND_MAIN}"业务生成' in prompt, "business 应回落到品牌主名"
    assert '为"本地服务"业务生成' not in prompt


def test_business_label_priority():
    assert title_context.business_label("断镯修复", BRAND_MAIN) == "断镯修复"
    assert title_context.business_label("", BRAND_MAIN) == BRAND_MAIN
    assert title_context.business_label(None, "", "") == "本地服务"


def test_inject_puts_context_before_body_not_after_tail():
    """上下文要插在【标题风格】之前：T 段以"只输出纯标题列表"收尾，
    放在它后面容易被模型当噪音忽略。"""
    prompt = "开头段\n\n【标题风格】\n- 结构要求\n\n只输出纯标题文本列表。"
    out = title_context.inject(prompt, "【品牌知识库】X")
    assert out.index("【品牌知识库】X") < out.index("【标题风格】") < out.index("只输出")


def test_inject_falls_back_to_append_for_custom_prompt():
    """用户自定义过 T 段 Prompt（没有锚点）时退化为追加，不能把整块丢掉。"""
    out = title_context.inject("我自己写的选题提示词", "【品牌知识库】X")
    assert out.startswith("我自己写的选题提示词")
    assert "【品牌知识库】X" in out


def test_brand_block_empty_when_no_brand():
    assert title_context.brand_block("", BRAND_MAIN) == ""
    assert title_context.audience_block("") == ""


def test_audience_block_keeps_full_multi_field_profile():
    """画像全文必须进 T 段：五类多值字段不能只喂前几条。

    事故：截断上限 900 是"画像只有十几个条目"时代的取值。真实画像会长成
    10 单值 + 五类多值（核心痛点/决策顾虑/使用场景/常见地点/拒绝理由），
    900 字符只够看到单值字段和前二十几条痛点 —— 后面四类**一条都进不了选题**，
    且截断点落在条目中间，切出 "- 和" 这种半条碎片。
    """
    # 量级对齐真实画像（10 单值 + 五类共约 170 条 ≈ 3.5K 字符）
    singles = "\n".join(f"【字段{i}】{'值' * 18}" for i in range(10))
    lists = "\n".join(
        f"【{label}】\n" + "\n".join(f"- {label}第{i}条{'内' * 8}" for i in range(34))
        for label in ("核心痛点", "决策顾虑", "使用场景", "常见地点", "拒绝理由"))
    content = singles + "\n" + lists
    assert 900 < len(content) < 6000, "该用例要长到能触发旧上限、又在新上限之内"

    block = title_context.audience_block(content)
    assert content in block, "画像被截断：多值字段（顾虑/场景/地点/拒绝理由）会静默丢失"
    # 每条都完整在场，不留被腰斩的半条
    for line in content.splitlines():
        if line.startswith("- "):
            assert line in block


def test_keep_question_only_strips_marketing_tail():
    """GEO 标题只留问句：问号后的"做法建议/卖点"一律砍掉。

    实测模型顽固沿用"前半句问法 + 后半句钩子"，靠 Prompt 收不住，这里确定性截断。
    """
    from app.services.writer.output_clean import keep_question_only

    assert (keep_question_only("上海翡翠手镯摔裂修复多少钱？先勘测再一口价")
            == "上海翡翠手镯摔裂修复多少钱？")
    assert keep_question_only("和田玉手镯有暗裂要不要修?别白花钱") == "和田玉手镯有暗裂要不要修?"
    # 没有问号的陈述式标题原样保留
    assert keep_question_only("玉镯断裂后修复的强度剩多少") == "玉镯断裂后修复的强度剩多少"
    assert keep_question_only("  玉镯裂纹修复要多久？  ") == "玉镯裂纹修复要多久？"
