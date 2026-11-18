# -*- coding: utf-8 -*-
"""「AI 指令自定义」板块的回归测试（2026-10-19 复查修复）。

外部复查（阶跃星辰）提了 10 条，落到代码上核实后**真的 8 条、已修/误报 2 条**，
本文件把真的那些钉住：

1. 页面「可用变量」名单与 Prompt 正文脱节（A 少 5 个、B 少 5 个、C 少 5 个）→
   `vars` 改成从正文解析；
2. 自定义指令里写错变量名会原样发给模型（不报错、不替换）→ 保存时拒绝；
3. 保存空内容被标成「已自定义」，实际跑的还是内置模板 → 拒绝 + 判据与生成侧同源；
4. B 段自定义后输出契约冲突 → 保存时如实提示；
5. `city_ratio` / `timeliness_ratio` 只存不用（页面却写"下次 AI 选题生效"）→ 已接线；
6. `expert` / `benefit_hints` 兜底是跨行业硬编码 → 改为跟业务定位同源；
7. 自定义 T 段时品牌/受众注入块退化成"追加到末尾" → 改为插到收尾指令之前。

误报的两条（七个维度未加权、C 段改完不复评）见 `test_review_compat.py`。
"""
from __future__ import annotations

from app.services.llm import LLMClient
from app.services.llm.keypool import KeyPool
from app.services.llm.providers import FakeProvider
from app.services.writer import title_context
from app.services.writer.pipeline import generate_titles
from app.services.writer.prompt_builder import PromptLibrary
from app.tenancy import tenant_scope


def _login(client, user="alpha", pw="pw-alpha"):
    r = client.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200


def _capturing(prompts: list[str]):
    def responder(messages):
        prompts.append(messages[-1]["content"])
        return "上海玉镯断裂修复多少钱？\n玉镯裂了怎么修？"

    return LLMClient(KeyPool(["k-test"]), FakeProvider(responder), max_retries=0)


# ── ① 变量清单与 Prompt 正文同源 ─────────────────────────────────────────────

def test_api_vars_match_prompt_body_exactly(client, app):
    """接口给出的可用变量必须**等于** Prompt 正文里真实出现的占位符。

    页面那份手写名单是漂移的源头：A 段实际用 11 个却只列 6 个、B 段 6 个列 1 个、
    C 段 8 个列 3 个 —— 用户照着名单写指令，会以为 {phone} / {brand_names}
    这些不存在。现在名单由正文解析，本用例就是防它再漂回去。
    """
    _login(client)
    body = client.get("/api/my-prompts").get_json()
    with app.app_context():
        lib = PromptLibrary()
        for step in ("A", "B", "C", "T"):
            got = [v["name"] for v in body["vars"][step]]
            assert got == lib.required(step), f"{step} 段变量清单与正文不一致"
            assert got, f"{step} 段不该是空清单"
            for v in body["vars"][step]:
                assert v["desc"], f"{step} 的 {v['name']} 缺中文说明"


def test_api_vars_cover_the_ones_the_page_used_to_miss(client):
    """点名钉住曾经缺失、但正文确实在用的变量。"""
    _login(client)
    body = client.get("/api/my-prompts").get_json()
    got = {s: {v["name"] for v in vs} for s, vs in body["vars"].items()}
    assert {"phone", "contact_rule", "current_period", "business_skeleton"} <= got["A"]
    assert {"brand_names", "brand_data", "phone", "contact_rule"} <= got["B"]
    assert {"brand_names", "phone", "business", "title", "contact_rule"} <= got["C"]
    # T 段点名 genre_mix（正文真实使用、页面名单漏列）；
    # benefit_hints 的退役终态见下方 test_t_benefit_hints_is_retired。
    assert "genre_mix" in got["T"]


def test_t_benefit_hints_is_retired(app):
    """`{benefit_hints}` 曾是「声明了但用不了」的变量：只在 T 段文末变量清单的
    HTML 注释里出现，正文一处都没引用（原钉桩用例 `..._is_declared_but_unusable`
    记录过这个待决项）。v1.1 收口按退役分支处理：注册表、T 段清单注释、生成侧
    注入点全部移除（见 spec/_共享/变量注册表.md 变更记录）。

    本用例钉住退役终态，防止它再"半接线"回来（登记了却没人消费，或注释复辟）。
    """
    import json
    from pathlib import Path

    from app.spec import prompts_files, variable_registry

    with app.app_context():
        lib = PromptLibrary()
        assert "benefit_hints" not in lib.required("T")
        raw = Path(prompts_files()["T"]).read_text(encoding="utf-8")
        assert "benefit_hints" not in raw, "T 段注释清单也不应再出现退役变量"
        reg = json.loads(Path(variable_registry()).read_text(encoding="utf-8"))
        assert "benefit_hints" not in {v["name"] for v in reg["variables"]}


# ── ② 未知变量：保存即拒绝 ───────────────────────────────────────────────────

def test_unknown_variable_is_rejected_on_save(client):
    """`{bran_data}` 这种笔误原来会被原样发给模型（既不报错也不替换）。"""
    _login(client)
    r = client.post("/api/my-prompts/A", json={"content": "按 {bran_data} 写，主题 {title}"})
    assert r.status_code == 400
    msg = r.get_json()["message"]
    assert "bran_data" in msg
    assert "{brand_data}" in msg, "报错要顺手告诉用户正确写法"


def test_known_variables_still_save(client):
    _login(client)
    r = client.post("/api/my-prompts/A", json={"content": "主题 {title}，品牌 {brand_data}"})
    assert r.get_json()["ok"] is True


def test_json_example_in_prompt_is_not_treated_as_variable(client):
    """指令里写 JSON 示例（`{"overall_score": 8}`）不能被当成变量误拒。"""
    _login(client)
    r = client.post("/api/my-prompts/B", json={
        "content": '输出 JSON：{"overall_score": 8, "dimension_scores": []}'})
    assert r.get_json()["ok"] is True


# ── ③ 空内容 ≠ 自定义 ────────────────────────────────────────────────────────

def test_empty_content_is_rejected(client):
    _login(client)
    r = client.post("/api/my-prompts/A", json={"content": "   \n  "})
    assert r.status_code == 400
    assert "恢复默认" in r.get_json()["message"]


def test_blank_custom_row_is_not_reported_as_custom(client, app):
    """历史数据：is_custom=True 但内容为空 → 页面必须如实显示「默认」。

    生成侧 `custom_text()` 对空内容回落内置模板；页面若仍标「已自定义」，
    用户会以为自己的指令生效了，其实一个字都没进 Prompt。
    """
    from app.extensions import db
    from app.models import Prompt
    from app.tenancy import TenantScopedRepository

    _login(client)
    with app.app_context(), tenant_scope("t_alpha"):
        TenantScopedRepository(Prompt).add(
            Prompt(tenant_id="t_alpha", step="A", name="生成器", content="", is_custom=True))
        db.session.commit()

    prompts = {p["step"]: p for p in client.get("/api/my-prompts").get_json()["prompts"]}
    assert prompts["A"]["is_custom"] is False
    assert prompts["A"]["content"].strip(), "应回显内置模板而非空串"


def test_empty_content_never_reaches_generation(client, app):
    """空内容行存在时，生成侧仍走内置模板（与页面显示一致）。"""
    from app.extensions import db
    from app.models import Prompt
    from app.services.writer.prompt_builder import custom_text
    from app.tenancy import TenantScopedRepository

    with app.app_context(), tenant_scope("t_alpha"):
        TenantScopedRepository(Prompt).add(
            Prompt(tenant_id="t_alpha", step="T", name="标题选题", content="  ", is_custom=True))
        db.session.commit()
        assert custom_text("T") is None


# ── ④ B 段 JSON 契约提示 ────────────────────────────────────────────────────

def test_b_step_without_json_gets_a_warning(client):
    _login(client)
    body = client.post("/api/my-prompts/B", json={"content": "严格审稿，指出所有问题"}).get_json()
    assert body["ok"] is True
    assert "JSON" in body["warning"]


def test_b_step_with_json_has_no_warning(client):
    _login(client)
    body = client.post("/api/my-prompts/B", json={
        "content": "严格审稿，按 JSON 格式输出，品牌资料见 {brand_data}"}).get_json()
    assert body["ok"] is True
    assert "warning" not in body


def test_a_step_never_carries_warning(client):
    """别的段不加 warning 字段（原站响应形状 {ok,length} 不能变）。"""
    _login(client)
    assert client.post("/api/my-prompts/A", json={"content": "写文章"}).get_json() == {
        "ok": True, "length": 3}


# ── ⑤ city_ratio / timeliness_ratio 接线 ────────────────────────────────────

def test_city_rule_text_follows_ratio_and_cities():
    assert "10 条中约 6 条带具体城市（城市词占比 60%）" in title_context.city_rule(10, 60, "上海")
    assert "10 条标题全部用泛地域表述" in title_context.city_rule(10, 0, "上海")
    assert "10 条标题全部带具体城市" in title_context.city_rule(10, 100, "上海")
    assert "未配置业务城市" in title_context.city_rule(10, 60, "")
    # 脏值不炸：回落到默认 60%
    assert "60%" in title_context.city_rule(10, "abc", "上海")


def test_timeliness_hint_text():
    assert "40%" in title_context.timeliness_hint(40)
    assert "不要求时效词" in title_context.timeliness_hint(0)
    assert "25%" in title_context.timeliness_hint("abc")


def test_ratios_actually_reach_t_prompt(app):
    """两个比例以前只在页面里存着，生成侧一条都没读 —— 现在必须进 Prompt。"""
    from app.extensions import db
    from app.models import TitleConfig
    from app.tenancy import TenantScopedRepository

    prompts: list[str] = []
    with app.app_context(), tenant_scope("t_alpha"):
        cfg = TenantScopedRepository(TitleConfig).first()
        cfg.cities = "上海市黄浦区 / 上海市徐汇区"
        cfg.city_ratio = 50
        cfg.timeliness_ratio = 40
        db.session.commit()
        generate_titles(10, llm=_capturing(prompts))

    prompt = prompts[0]
    assert "10 条中约 5 条带具体城市（城市词占比 50%）" in prompt, "city_ratio 没生效"
    assert "另有约 40% 的标题带时效词前缀" in prompt, "timeliness_ratio 没生效"


# ── ⑥ 兜底不再跨行业 ────────────────────────────────────────────────────────

def test_expert_and_benefit_hints_fall_back_to_business_not_cross_industry(app):
    """`expert` 兜底曾是"本地生活服务搜索营销专家"、`benefit_hints` 曾是
    "专业资质、透明报价、售后保障" —— 玉石修复租户拿到装修/本地服务的用词。"""
    from app.extensions import db
    from app.models import TitleConfig
    from app.tenancy import TenantScopedRepository

    prompts: list[str] = []
    with app.app_context(), tenant_scope("t_alpha"):
        cfg = TenantScopedRepository(TitleConfig).first()
        cfg.expert = ""
        cfg.business = ""
        db.session.commit()
        generate_titles(2, llm=_capturing(prompts))

    prompt = prompts[0]
    assert "本地生活服务搜索营销专家" not in prompt
    assert "专业资质、透明报价、售后保障" not in prompt
    assert "你是阿尔法珠宝领域的行业说明者" in prompt, "expert 应回落到品牌主名"


def test_benefit_hints_cross_industry_literal_is_gone():
    """`benefit_hints` 的兜底不再是硬编码的跨行业促销词（装修/本地服务口径）。

    该变量本身已按 v1.1 收口退役 —— 见 `test_t_benefit_hints_is_retired`。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "app/services/writer/pipeline.py").read_text(encoding="utf-8")
    assert 'or "专业资质' not in src


# ── ⑦ 自定义 T 段时的注入位置 ───────────────────────────────────────────────

def test_inject_uses_secondary_anchor():
    prompt = "开头。\n\n【本次选题方向】\n- 价格\n\n只输出纯标题列表。"
    out = title_context.inject(prompt, "【品牌知识库】X")
    assert out.index("【品牌知识库】X") < out.index("【本次选题方向】")


def test_inject_inserts_before_tail_instruction_when_no_section_anchor():
    """自定义过的 T 段往往没有【标题风格】小节。原来直接追加到末尾，于是
    上下文块落在"只输出纯标题文本列表，不要输出任何思考过程"**之后** ——
    模型很容易把它当成不该输出的内容而忽略。"""
    prompt = "你是一个选题助手。\n请产出 10 个标题。\n只输出纯标题文本列表，不要输出任何思考过程。"
    out = title_context.inject(prompt, "【品牌知识库】X")
    assert out.index("【品牌知识库】X") < out.index("只输出纯标题文本列表")
    assert out.startswith("你是一个选题助手。")


def test_inject_still_appends_when_nothing_resembles_an_anchor():
    out = title_context.inject("我自己写的选题提示词", "【品牌知识库】X")
    assert out.startswith("我自己写的选题提示词")
    assert "【品牌知识库】X" in out


# ── ⑧ 保存/重置的既有语义不回退 ─────────────────────────────────────────────

def test_reset_returns_to_default(client):
    _login(client)
    client.post("/api/my-prompts/A", json={"content": "自定义"})
    assert client.post("/api/my-prompts/A/reset").get_json() == {"ok": True}
    prompts = {p["step"]: p for p in client.get("/api/my-prompts").get_json()["prompts"]}
    assert prompts["A"]["is_custom"] is False