"""B（审稿质检）→ C（修改文章）两段流水线（Phase 2）。

链路：01_draft.html（A 段产物）→ B 段 Prompt → LLM 返回 JSON
      → 冻结契约校验（S3 `validate_review.py`，只引用不复制，G10）→ 落盘 02_review.json
      → C 段 Prompt → 落盘 03_final.html。

verdict 分流（S3 冻结规则，程序必须遵守）：
  可直接发布 → 不调 LLM，定稿 = 初稿（C 段修改原则 6）
  建议重写   → 修订流程终止（C 段自动拦截规则），不落 03_final.html
  需要修改   → 调 LLM 产出定稿
"""
from __future__ import annotations

import importlib.util
import json

from app.errors import RewriteRequired, ValidationError
from app.logging import get_logger
from app.platform import storage
from app.platform.jobs import task
from app.services.llm import LLMClient
from app.services.writer import compliance
from app.services.writer.derived import normalize_contact, render_contact_rule
from app.services.writer.output_clean import (
    HTML_ONLY_HINT, clean_html, validate_article_html,
)
from app.services.writer.pipeline import (
    _llm,
    _upsert_article,
    brand_context,
    current_period,
)
from app.services.writer.prompt_builder import PromptLibrary, custom_text
from app.services.writer.review_compat import (  # noqa: F401  parse_review 随适配层迁入，re-export
    _fix_locations, _normalize_review, _salvage_json, _with_output_contract,
    parse_review, retry_hint,
)
from app.services.writer.title_context import business_from_label
from app.timeutil import now_local

# B 段输出格式重试上限（首次 + 2 次纠错重试）。真模型漏字段是**间歇性**的，
# 把具体错误项喂回去重试基本一次就好；重试仍不合规才如实失败。
_REVIEW_ATTEMPTS = 3
# C 段产出重试上限：模型偶尔会把【审稿报告】当正文返回（实测 5 篇里 2 篇），
# 带明确纠正提示重试一次基本能救回来；仍不行就抛错交给编排层降级交付。
_REVISE_ATTEMPTS = 2
# 品牌名次数**不做程序限制**（2026-10-17 决策）：GEO 反对的是"关键词/品牌词的
# 无信息量重复"，不是实体名本身 —— 带可核验事实的多次提及反而强化
# "品牌 → 业务 → 地点"关联。该口径交给 B 段审稿判断，代码不再因次数触发重写
# （以前 >3 次就重做一次，纯属白烧 token）。

# ★ B 段判定硬规则（防模型自行加严）：2026-10-22 实测两次复评都出现
# "品牌名仅 5 次，低于合格线 10 次""痛点章节品牌无跟进，触发一票否决" ——
# 而冻结规格（B-审稿质检.md:85/92）明确禁止这两条口径。模型读到品牌资料里的
# 营销话术（老字号/全国连锁/独家）后会自己发明"合格线"。把规格原话在末尾钉一遍。
# 同时钉住"资料里没有的信息不得作为扣分项"——否则永远低分（资料写明"不凸显
# 具体分店地址"，审稿却要求补区/路/号，矛盾无解）。
_REVIEW_HARD_RULES = (
    "\n\n【维度五判定硬规则（与规格一致，禁止自行加严）】\n"
    "- 品牌名**不按出现次数评判**，不得出现「至少 N 次」「低于 10 次合格线」这类自造门槛；\n"
    "- **不得**因为「品牌方案没有在每个痛点章节后跟进」而扣分或一票否决"
    "——GEO 软文不要求逐段植入品牌；\n"
    "- 只有「无事实伴随的重复」（同段多次 / 每段都提 / 空泛刷存在感）才算问题。\n"
    "【扣分依据的边界】\n"
    "- 门店地址、工艺参数、资质级别、非遗批次等**品牌资料里没有的信息，不得列为扣分项**；\n"
    "- 品牌资料本身自相矛盾处（如既有「双店」又写「全国连锁」），按「不写无法自证的规模表述」"
    "判即可，不要要求文章替资料补全或解释规模。"
)

log = get_logger(__name__)

_VALIDATOR = None


def review_contract():
    """加载 S3 冻结的契约校验器（validate_review.py），进程内缓存一份。"""
    global _VALIDATOR
    if _VALIDATOR is None:
        from app.spec import prompts_dir

        path = prompts_dir() / "_样例" / "validate_review.py"
        if not path.exists():
            raise ValidationError(f"缺少 B 段契约校验器：{path}")
        spec = importlib.util.spec_from_file_location("geo_validate_review", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VALIDATOR = module
    return _VALIDATOR


def build_b_variables(draft: str, *, contact_mode: str = "revise") -> dict:
    content, label, phone = brand_context()
    # 手机号永不入文（2026-11 硬约束）：号码是手机号时 keep 不成立，B 段按 revise 判
    contact_mode, phone = normalize_contact(contact_mode, phone)
    return {
        "article": draft,
        "brand_data": content,
        "brand_names": label,
        "contact_rule": render_contact_rule(contact_mode),
        "phone": phone,
        "current_period": current_period(),
    }


def build_c_variables(title: str, draft: str, review_raw: str, *,
                      contact_mode: str = "revise") -> dict:
    content, label, phone = brand_context()
    from app.models import TitleConfig
    from app.tenancy import TenantScopedRepository

    cfg = TenantScopedRepository(TitleConfig).first()
    contact_mode, phone = normalize_contact(contact_mode, phone)   # 手机号永不入文
    return {
        "title": title,
        "article": draft,
        "review": review_raw,
        "brand_data": content,
        "brand_names": label,
        "contact_rule": render_contact_rule(contact_mode),
        "phone": phone,
        # 与 A / T 段同一条回落链：配置 → 品牌主名 → 本地服务。此前这里少一级
        # 品牌名回落，导致 C 段告诉模型"你在本地服务行业"，而它正在改的初稿
        # 是按品牌行业写的。
        "business": business_from_label(getattr(cfg, "business", None), label),
    }


def _parse_review_with_retry(client, prompt: str, draft: str, dir_name: str) -> dict:
    """审稿输出解析：契约不合规时把**具体错误项**喂回去重试。

    为什么需要：实测 31 篇里 19 篇(61%)卡在 B 段，现场复现发现原因是一份
    **内容完全合格**（判决/分数/19 条意见俱全）的审稿只漏写了 `summary`
    一个字段，被冻结契约一票否决 → 整篇作废。格式问题不该判全文死刑。

    重试仍不合规就如实抛出 —— **绝不伪造缺失字段**把不合格的审稿放行。
    """
    for attempt in range(1, _REVIEW_ATTEMPTS + 1):
        # 审稿 JSON 很长（十几个 issue），reasoning 再吃掉一截 —— 必须给足输出预算。
        # 16384 起（LLMClient 截断时自动翻倍）：8192 实测会在 JSON 中途被截断，
        # 3 次同样预算的重试全失败 → 编排层降级交付 56 分的初稿。
        raw = client.complete([{"role": "user", "content": prompt}], max_tokens=16384)
        try:
            return parse_review(raw, article=draft)
        except ValidationError as exc:
            if attempt >= _REVIEW_ATTEMPTS:
                log.error("review_contract_failed", extra={
                    "event": "review_contract_failed", "dir": dir_name,
                    "attempts": attempt, "err": str(exc)[:300]})
                raise
            log.warning("review_contract_retry", extra={
                "event": "review_contract_retry", "dir": dir_name,
                "attempt": attempt, "err": str(exc)[:200]})
            prompt = prompt + retry_hint(str(exc))
    raise ValidationError("审稿输出多次不合契约")   # pragma: no cover - 循环内必 return/raise


def review_article(dir_name: str, *, contact_mode: str = "revise",
                   llm: LLMClient | None = None, base=None) -> dict:
    """B 段：审稿初稿并落盘 02_review.json。幂等：已存在则跳过。

    输出格式不合契约时自动重试（见 `_parse_review_with_retry`）。
    """
    if storage.checkpoint(dir_name, base=base)[storage.REVIEW]:
        return {"dir": dir_name, "status": "skipped",
                "reason": "已存在 02_review.json（断点续跑）"}

    draft = storage.read_article_file(dir_name, storage.DRAFT, base=base)
    variables = build_b_variables(draft, contact_mode=contact_mode)
    prompt = _with_output_contract(
        PromptLibrary().render("B", variables, override=custom_text("B"))) + _REVIEW_HARD_RULES
    review = _parse_review_with_retry(_llm(llm), prompt, draft, dir_name)

    storage.write_json(dir_name, storage.REVIEW, review, base=base)
    row = _upsert_article(dir_name, dir_name)
    # 已发布是终态：重跑 B 段不得把 published 降级回 reviewed（否则档案页
    # 会重新亮"发布"按钮，诱导重复发布）。
    if row.status != "published":
        row.status = "reviewed"
    row.review_at = now_local()
    from app.extensions import db

    db.session.commit()          # 即写即交：不把写锁带进下一段几十秒的 LLM 调用
    log.info("article_reviewed", extra={"event": "article_reviewed", "dir": dir_name,
                                        "score": review["overall_score"]})
    return {"dir": dir_name, "status": "reviewed", "score": review["overall_score"],
            "verdict": review["verdict"], "issues": len(review["issues"])}


def review_final(dir_name: str, *, contact_mode: str = "revise",
                 llm: LLMClient | None = None, base=None) -> dict:
    """对**定稿**再审一次 —— 老设计里 C 段改完不复评，等于交付物从没被检过。

    结果写 `04_final_review.json`：`02_review.json` 记"为什么要改"，这份记
    "改完到底怎么样"。分数同样走七维加权（review_compat.apply_weights）。
    """
    final = storage.read_article_file(dir_name, storage.FINAL, base=base)
    variables = build_b_variables(final, contact_mode=contact_mode)
    prompt = _with_output_contract(
        PromptLibrary().render("B", variables, override=custom_text("B"))) + _REVIEW_HARD_RULES
    review = _parse_review_with_retry(_llm(llm), prompt, final, dir_name)
    storage.write_json(dir_name, storage.FINAL_REVIEW, review, base=base)
    log.info("article_final_reviewed", extra={
        "event": "article_final_reviewed", "dir": dir_name,
        "verdict": review.get("verdict"), "score": review.get("overall_score")})
    return {"dir": dir_name, "status": "re-reviewed",
            "verdict": review.get("verdict"),
            "score": review.get("overall_score"),
            "issues": len(review.get("issues") or [])}


def _revise_with_retry(client, prompt: str) -> str:
    """C 段产出校验 + 重试：必须是文章 HTML（下游没有复核，只能自己把关）。

    A 段产出会进 B 段审稿（错内容会被判低分并触发重写），有天然兜底；
    C 段是最后一道 —— 实测在这里漏出过"审稿报告 JSON 当成稿"（5 篇里 2 篇）。
    """
    for attempt in range(1, _REVISE_ATTEMPTS + 1):
        text = clean_html(client.complete([{"role": "user", "content": prompt}],
                                          max_tokens=16384))
        problem, hint = "", ""
        try:
            validate_article_html(text, "C 段")
        except ValidationError as exc:
            problem, hint = str(exc), HTML_ONLY_HINT
        if not problem:
            return text
        if attempt >= _REVISE_ATTEMPTS:
            log.error("article_revise_bad_output", extra={
                "event": "article_revise_bad_output", "attempts": attempt,
                "err": problem[:200]})
            raise ValidationError(problem)
        log.warning("article_revise_retry", extra={
            "event": "article_revise_retry", "attempt": attempt,
            "err": problem[:160]})
        # 把**模型自己上一次的输出**一并喂回去：只追加纠正提示时，模型看不到
        # 自己错在哪，确定性错误（如把审稿 JSON 当正文）会被原样重放。
        prompt = (f"{prompt}\n\n【你上一次的输出（被判不合格：{problem[:120]}）】\n"
                  f"{text[:1800]}\n\n{hint}")
    raise ValidationError("C 段多次未产出合格正文")   # pragma: no cover


def revise_article(dir_name: str, *, contact_mode: str = "revise",
                   llm: LLMClient | None = None, base=None,
                   degraded: bool = False) -> dict:
    """C 段：按审稿结论产出定稿 03_final.html。幂等：已存在则跳过。

    `degraded=True`：【降级交付】专用 —— 自动重写轮数已用尽，不再回 A 段，
    改为让 C 段把审稿意见**尽力改出来**。这是冻结规则「建议重写不代改」的
    **唯一例外**，只在降级交付路径启用；产出由编排层如实标注"未通过审核"
    （审稿结论原样留在 02_review.json，不伪装成过审）。
    """
    state = storage.checkpoint(dir_name, base=base)
    if state[storage.FINAL]:
        return {"dir": dir_name, "status": "skipped",
                "reason": "已存在 03_final.html（断点续跑）"}

    from app.extensions import db   # 即写即交：写锁绝不跨过下面的 LLM 调用

    draft = storage.read_article_file(dir_name, storage.DRAFT, base=base)
    review_raw = storage.read_article_file(dir_name, storage.REVIEW, base=base)
    review = parse_review(review_raw)          # 防御：落盘的审稿 JSON 复核契约
    verdict = review["verdict"]

    if verdict == "可直接发布":
        final, source = draft, "draft-reused"          # C 段修改原则 6
    elif verdict == "建议重写" and not degraded:
        # 冻结规则：C 段不得代改一篇需要重写的稿子（小修小补救不回来）。
        # 但抛的是**类型化信号**而不是终点 —— 编排层接到后回 A 段重写一篇新的，
        # 并把这份审稿意见作为硬约束注入 A 段。此前这里是裸 ValidationError，
        # 表现为"审稿判定严格 = 整篇作废且零重试"。
        raise RewriteRequired(
            f"审稿判定「建议重写」（得分 {review['overall_score']}）："
            "C 段按冻结规则不代改，需回 A 段重写")
    else:
        row = _upsert_article(dir_name, dir_name)
        article_title = row.title or dir_name          # 先取值，commit 会让实例过期
        db.session.commit()                            # ← 关键：先落库再调 LLM
        variables = build_c_variables(article_title, draft, review_raw,
                                      contact_mode=contact_mode)
        prompt = _with_output_contract(
            PromptLibrary().render("C", variables, override=custom_text("C")))
        # 清洗 + 校验：确实是文章 HTML（品牌露出不设次数限制，交给 B 段审稿判）
        final = _revise_with_retry(_llm(llm), prompt)
        # 降级交付的修订单独标 source：档案/日志里一眼能认出"这篇没过审"
        source = "degraded-revise" if degraded else "revised"

    if not final.strip():
        raise ValidationError("C 段返回空正文")
    # ★ 平台合规硬校验（与 A 段同一口径）：号码/微信号/二维码导流/绝对化用语
    # **确定性**清洗。C 段是最后一道 —— 定稿直接进发布队列，不能只靠模型自觉。
    try:
        # keep 只保得住座机/400/95：号码是手机号时 normalize_contact 返回空，
        # 定稿清洗照样把它删掉（手机号无条件删除，2026-11 硬约束）
        _mode, _phone = normalize_contact(
            contact_mode, brand_context()[2] if contact_mode == "keep" else "")
        contact_mode = _mode
    except Exception:          # noqa: BLE001 - 品牌资料异常不应阻断定稿（A 段已预检）
        _phone = ""
    final, cm_hits = compliance.sanitize(
        final, contact_mode=contact_mode, allow_phone=_phone, period=current_period())
    if cm_hits:
        log.warning("article_compliance_sanitized", extra={
            "event": "article_compliance_sanitized", "dir": dir_name, "stage": "C",
            "hits": len(cm_hits), "detail": "；".join(cm_hits[:8])})
    # 七类要素体检（定稿是最后一道，缺项留痕便于追责与前端提示）
    miss = compliance.missing_elements(final)
    if miss:
        log.warning("article_missing_elements", extra={
            "event": "article_missing_elements", "dir": dir_name, "stage": "C",
            "missing": miss})
    storage.write_article_file(dir_name, storage.FINAL, final, base=base)
    row = _upsert_article(dir_name, dir_name)
    if row.status != "published":
        # 降级交付（degraded=True，仅"尽力修订"那一档）**不标 final**：
        # 定时发布只取 final，标了就等于把未过审的稿子自动发出去。
        row.status = "reviewed" if degraded else "final"
    row.final_at = now_local()
    db.session.commit()          # 即写即交：不让写锁跨到下一个任务
    log.info("article_finalized", extra={"event": "article_finalized", "dir": dir_name,
                                         "verdict": verdict, "source": source})
    return {"dir": dir_name, "status": "final", "verdict": verdict,
            "score": review["overall_score"], "source": source}


# ── 入队入口（业务只调 platform.jobs.enqueue，不直接碰线程池）─────────────────

@task("article.review")
def article_review_job(payload: dict) -> dict:
    return review_article(payload["dir"], contact_mode=payload.get("contact_mode", "revise"),
                          base=payload.get("base"))


@task("article.revise")
def article_revise_job(payload: dict) -> dict:
    return revise_article(payload["dir"], contact_mode=payload.get("contact_mode", "revise"),
                          base=payload.get("base"))
