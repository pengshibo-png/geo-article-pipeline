"""T→A 生成闭环（Phase 1 最小垂直切片）。

链路：选题（T 段可批量产出，亦可从选题库取）→ A 段渲染 Prompt → 经多 Key 池调 LLM
      → 落盘 `01_draft.html` → 写检查点（断点续跑）。
"""
from __future__ import annotations

import random
import re
from datetime import datetime

from flask import current_app

from app.errors import ConfigError, ValidationError
from app.logging import get_logger
from app.models import Article, Asset, Audience, Brand, GenreMix, Title, TitleConfig
from app.platform import storage
from app.platform.jobs import task
from app.services.llm import LLMClient, build_llm_client
from app.services.writer import compliance, title_context
from app.services.writer.derived import (derive_brand_names, extract_phone,
                                         normalize_contact, render_contact_rule)
from app.services.writer.output_clean import clean_html, clean_title_line, keep_question_only
from app.services.writer.prompt_builder import PromptLibrary, citable_facts, custom_text
from app.tenancy import TenantScopedRepository
from app.timeutil import now_local, shanghai

log = get_logger(__name__)

# A 段正文字数下限（规格 A-写文章.md §三：总字数 2000–3500 字）。
# 低于下限时带差额提示重写一次 —— 实测 1700 余字收尾会被审稿判"信息密度不足"。
_MIN_CHARS = 2000

# 交付前自查清单：把规格里的**硬数字**在 Prompt 末尾再点一遍。
# 模型对末尾指令更敏感 —— 实测正文常停在 1700 余字（低于 2000 下限），
# 而"字数不足 + 信息密度不够"正是审稿打 56 分、触发整轮重写的主因。
def _quality_block(period: str) -> str:
    """七类要素取自规格 §三「七类要素一件都不能少」（A-写文章.md:136-137）。

    漏写任一项都会被 B 段维度三判低分（实测：缺表格/定义句/时效信号 → 维度三 62 分），
    所以在末尾完整列一遍，让 A 段一次写全，不必靠审稿打回重写。
    """
    p = period or "当前年月"
    return (
        "【交付前自查（不满足就返工，不要输出自查过程）】\n"
        "- 正文**不得少于 2000 字**（区间 2000–3500；宁可多写依据，不要提前收尾）；\n"
        "- **七类要素一件都不能少**：\n"
        "  ① 定义句：关键概念以「XX，是指……」出现在**段落开头**；\n"
        "  ② 结论句：首段一句话结论，可脱离上下文独立成立，用 <strong> 加粗；\n"
        "  ③ **表格（≥3 行，用 <table>）**：参数 / 对比 / 价格档位任选一组做成表；\n"
        "  ④ 编号列表（≥3 步，用 <ol>）；\n"
        "  ⑤ 适用边界：**必须**有一句写清哪种情况**不建议**做，例如"
        "「这类情况不建议直接修：……」或「如果……，建议先别修」；\n"
        "  ⑥ Q&A 小节：**必须**以 `<h2>常见问题</h2>` 开头，紧跟 2–3 组，"
        "格式照抄：`<p><strong>Q：……？</strong></p><p>A：……（第一句直接给结论）</p>`；\n"
        f"  ⑦ 时效信号：只用「{p}」口径，**不得写未来时间**；\n"
        "- 4–6 个 <h2> 小节，每节「先结论 → 再依据（参数 / 流程 / 对比）→ 一句可引用的总结」；\n"
        "- 量化信息 ≥2 处（参数 / 时长 / 温度 / 比例 / 区间）；\n"
        "- 权威来源 ≥1 处（国标号或机构名）——**必须真实存在，拿不准就不写**。"
    )


TZ = shanghai()

from app.services.writer.angles import (  # noqa: F401  再导出：既有调用方仍从 pipeline 取
    ANGLES,
    ANGLE_META,
    current_period,
    pick_angle,
    render_angle,
)

_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")
def slugify(title: str, *, max_len: int = 40) -> str:
    """文章目录名 = 标题去标点（施工规格书命名规则）。"""
    return (_PUNCT.sub("", title or "")[:max_len]) or "untitled"
def _settings():
    return current_app.config["GEO_SETTINGS"]


def _llm(llm: LLMClient | None) -> LLMClient:
    return llm or build_llm_client(_settings())


def brand_context() -> tuple[str, str, str]:
    """返回 (brand_data, brand_names_label, phone)。主名识别失败 → 阻塞（brand-names.md R2）。"""
    brand = TenantScopedRepository(Brand).first()
    content = (brand.content if brand else "") or ""
    names = derive_brand_names(content)
    if not names["ok"]:
        raise ValidationError("品牌资料「品牌概述」首句无法识别主名，按 R2 阻塞生成")
    label = str(names["main"])
    if names["alias"]:
        label += f"（{names['alias']}）"
    return content, label, extract_phone(content)


def _city_context(title: str) -> str:
    cfg = TenantScopedRepository(TitleConfig).first()
    raw = (getattr(cfg, "cities", None) or "") if cfg else ""
    cities = [c.strip() for c in re.split(r"[,\n]", raw) if c.strip()]
    for city in cities:
        if city in title:
            return f"本文锚定城市：{city}"
    return f"本文泛地域参考城市：{cities[0]}" if cities else "本文不限城市"


def build_a_variables(title: str, *, contact_mode: str = "revise",
                      angle_id: str | None = None, genre: str | None = None,
                      image_html: str = "", business_skeleton: str = "") -> dict:
    content, label, phone = brand_context()
    audience = TenantScopedRepository(Audience).first()
    # {business}：A 段用它交代"在什么领域从业"。回落链与 T / C 段共用一份实现
    # （title_context.business_from_label = 配置 → 品牌主名 → 本地服务）。
    # 别在这里再写一遍 —— 三段各写一份的结果是同一篇文章里 {business} 各不相同。
    cfg = TenantScopedRepository(TitleConfig).first()
    business = title_context.business_from_label(getattr(cfg, "business", None), label)
    # 手机号永不入文：品牌电话若是手机号，keep 不成立（号码置空 + 降为 revise）
    contact_mode, phone = normalize_contact(contact_mode, phone)
    return {
        "title": title,
        "brand_data": content,
        "business": business,
        "audience": (audience.content if audience else "") or "",
        "city_context": _city_context(title),
        "angle": render_angle(angle_id or pick_angle(len(title)), genre),
        # contact_rule 与 phone 必须成对注入（contact-rule.md R6）
        "contact_rule": render_contact_rule(contact_mode),
        "phone": phone,
        "business_skeleton": business_skeleton,
        "current_period": current_period(),
        "image_html": image_html,
    }


def _take_title() -> tuple[str, str | None]:
    repo = TenantScopedRepository(Title)
    row = repo.first(used=False)
    if row is None:
        raise ValidationError("选题库没有可用标题：请先生成标题或手工录入")
    row.used = True
    return row.text, row.genre


def pick_image_html() -> str:
    """素材库随机取一张图（原站规则：文章自动取用配图）。无素材 → 空串。"""
    rows = TenantScopedRepository(Asset).all()
    if not rows:
        return ""
    a = random.choice(rows)
    return f'<img src="/media/{a.rel_path}" alt="" style="max-width:100%">'


def _upsert_article(dir_name: str, title: str) -> Article:
    repo = TenantScopedRepository(Article)
    row = repo.first(dir=dir_name)
    if row is None:
        row = repo.add(Article(dir=dir_name, title=title, status="draft",
                               draft_at=now_local()))
    return row


def generate_article(*, title_text: str | None = None, contact_mode: str = "revise",
                     angle_id: str | None = None, genre: str | None = None,
                     image_html: str = "", business_skeleton: str = "",
                     rewrite_note: str = "",
                     base=None, llm: LLMClient | None = None) -> dict:
    """T→A：产出一篇初稿。幂等：目录已存在 01_draft.html 则跳过（断点续跑）。

    `rewrite_note`：上一轮审稿意见（重写环注入）。**追加在 Prompt 末尾**而不改
    A 段规格文件本身 —— 与 B/C 段的 `_with_output_contract` 同一约定
    （冻结规格文件只读，运行期约束加在外层）。
    """
    lib = PromptLibrary()
    if not title_text:
        title_text, genre = _take_title()
        # ★ 年份兜底：**只补"从选题库取题"这条路径**（存量选题建库时没年份）。
        # 显式传入 title 的调用（断点续跑 / 单篇重跑）必须原样使用 ——
        # 给它加年份会改变 slugify 结果，续跑就找不到原目录了。
        title_text = title_context.normalize_title(title_text)

    # 标题合规（四个平台都单独管标题）：装饰符号、极限词、标题党标点、超长。
    # 不阻断生成（标题来自选题库，改了会影响选题对应关系），但必须留痕 ——
    # 网易号把「标题含特殊符号/过于夸张」直接算低质，头条号按标题党扣信用分。
    tp = compliance.title_problems(title_text)
    if tp:
        log.warning("article_title_noncompliant", extra={
            "event": "article_title_noncompliant", "title": title_text,
            "problems": tp})

    dir_name = slugify(title_text)
    if storage.checkpoint(dir_name, base=base)[storage.DRAFT]:
        # ★ 幂等只对**同一篇**成立。slugify 截断 40 字，长标题前 40 字相同会撞到
        # 同一个目录；此时若照旧跳过，任务会报「✅ 完成」而交付的是**另一篇旧文章**
        # （选题被白白消耗，用户以为发的是新的）。标题不一致就换带后缀的新目录。
        row = TenantScopedRepository(Article).first(dir=dir_name)
        owner = str((row.title if row else "") or "")
        if not owner or owner == title_text:
            return {"dir": dir_name, "title": title_text, "status": "skipped",
                    "reason": "已存在 01_draft.html（断点续跑）"}
        stem = slugify(title_text, max_len=36)
        for i in range(2, 10):
            cand = f"{stem}-{i}"
            if not storage.checkpoint(cand, base=base)[storage.DRAFT]:
                log.warning("article_dir_collision", extra={
                    "event": "article_dir_collision", "title": title_text,
                    "taken": dir_name, "use": cand})
                dir_name = cand
                break
        else:
            raise ValidationError(f"目录名冲突且后缀已用尽：{stem}")

    variables = build_a_variables(
        title_text, contact_mode=contact_mode, angle_id=angle_id, genre=genre,
        image_html=image_html or pick_image_html(), business_skeleton=business_skeleton,
    )
    prompt = lib.render("A", variables, override=custom_text("A"))
    facts = citable_facts()
    if facts:
        # 事实库：让"不编造"变成可执行 —— 给它一份能引用的清单，而不是只禁止
        prompt = f"{prompt}\n\n【可引用事实库·只能引用这里的事实，其余一律不许写】\n{facts}"
    if rewrite_note:
        prompt = f"{prompt}\n\n{rewrite_note}"
    # ★ 平台合规红线（网易号/搜狐号/百家号/头条号 共同要求）+ 交付前自查硬指标：
    # 作为附块注入，不改冻结规格文件 —— 与 rewrite_note / C 段 _with_output_contract
    # 同一约定（冻结规格只读，运行期约束加在外层）。
    prompt = (f"{prompt}\n\n{compliance.platform_block()}\n\n"
              f"{_quality_block(str(variables.get('current_period') or ''))}")
    # 真模型常见病（围栏/首尾寒暄）先清洗再落盘，否则脏包装会进初稿、被审稿当正文判
    # 2000-3500 字的正文 + reasoning 会吃掉大量输出预算，必须显式给上限。
    # 16384 起：实测 deepseek-flash 是 reasoning 模型，8192 会被思维链吃掉一半后
    # 在 2000 字附近被截断（finish_reason=length）；LLMClient 会在截断时自动翻倍。
    draft = clean_html(_llm(llm).complete([{"role": "user", "content": prompt}],
                                          max_tokens=16384))
    if not draft.strip():
        raise ValidationError("LLM 返回空正文")
    # fake 供应商的占位回显：配置丢失时会静默发生（默认就是 fake）。与"抖音电话
    # 填占位号"同类 —— 占位值悄悄覆盖真实产出，必须拦住（test 环境除外）。
    if "[FAKE]" in draft and getattr(current_app.config.get("GEO_SETTINGS"), "env", "") != "test":
        raise ConfigError("当前 LLM 是 fake 演示模式，产出的是占位正文；"
                          "请在「系统设置」配置真实模型后再生成")
    # ★ 平台合规硬校验：号码/微信号/二维码导流/绝对化用语**确定性**清洗。
    # Prompt 约束靠模型自觉（实测后半段会衰减），这里是发布前的最后一道闸。
    draft, cm_hits = compliance.sanitize(
        draft, contact_mode=contact_mode,
        allow_phone=str(variables.get("phone") or ""),
        period=str(variables.get("current_period") or ""))
    if cm_hits:
        log.warning("article_compliance_sanitized", extra={
            "event": "article_compliance_sanitized", "dir": dir_name, "stage": "A",
            "hits": len(cm_hits), "detail": "；".join(cm_hits[:8])})
    # ★ 字数体检：规格要求正文 2000–3500 字。实测模型常 1700 余字就收尾，
    # "字数不足 + 信息密度不够"是审稿给低分（56 分 → 建议重写）的主因。
    # 这里**只告警不重写**：A 段内重写会与既有重写环（最多 3 轮）重复计费，
    # 真正兜底靠下面注入的 QUALITY_BLOCK + 重写环把审稿意见带回 A 段。
    n_chars = compliance.visible_chars(draft)
    if n_chars < _MIN_CHARS:
        log.warning("article_too_short", extra={
            "event": "article_too_short", "dir": dir_name, "chars": n_chars,
            "floor": _MIN_CHARS})
    # 七类要素体检（可确定性判定的那几项）：缺项留痕，交给 B 段审稿扣分 + C 段补。
    miss = compliance.missing_elements(draft)
    if miss:
        log.warning("article_missing_elements", extra={
            "event": "article_missing_elements", "dir": dir_name, "stage": "A",
            "missing": miss})
    storage.write_article_file(dir_name, storage.DRAFT, draft, base=base)
    _upsert_article(dir_name, title_text)
    # 立刻提交：SQLAlchemy 的事务一旦开始就持到 run_job 结尾，而后面还有 B/C
    # 两段几十秒的 LLM 调用 —— 写锁会被跨着网络等待一直攥住，4 个 worker
    # 互相堵死（实测 database is locked）。写库必须"即写即交"。
    from app.extensions import db

    db.session.commit()
    log.info("article_drafted", extra={"event": "article_drafted", "dir": dir_name})
    return {"dir": dir_name, "title": title_text, "status": "drafted",
            "chars": len(re.sub(r"<[^>]+>", "", draft))}


def generate_titles(count: int = 5, *, llm: LLMClient | None = None) -> list[str]:
    """T 段：批量产出标题并入库（AI 选题的最小闭环）。

    查重：跳过库内已有与批内重复的文本（title 有 (tenant,text,deleted_at) 唯一约束，
    LLM 复述旧标题会 IntegrityError；页面语义是"本轮没有新选题通过查重"）。
    """
    lib = PromptLibrary()
    cfg = TenantScopedRepository(TitleConfig).first()
    mixes = TenantScopedRepository(GenreMix).all()
    existing_rows = TenantScopedRepository(Title).all()
    existing_list = [t.text for t in existing_rows][:120]
    existing = {t.text for t in existing_rows}
    mix_text = "；".join(f"{m.genre} {m.ratio:g}%" for m in mixes) or "商业推荐 100%"
    # 品牌库 / 受众：T 段 Prompt 的 11 个变量里**没有这两个**（规格正文却要求
    # "品牌名最多 1 条"），所以靠注入块补上。不注入的话 business 会兜底成
    # "本地服务" —— 实测玉石修复品牌被生成出"上门安装怎么选"这类无关标题。
    brand_content, brand_main, brand_alias = title_context.brand_snapshot()
    audience = TenantScopedRepository(Audience).first()
    cities = getattr(cfg, "cities", None) or ""
    # 业务定位先解析：expert / directions 的兜底都跟它同源
    business = title_context.business_label(
        getattr(cfg, "business", None), brand_main, brand_alias)
    variables = {
        # 兜底原写死"本地生活服务搜索营销专家"，跨行业身份会污染标题用词
        "expert": getattr(cfg, "expert", None) or f"{business}领域的行业说明者",
        "business": business,
        "count": count,
        "cities": cities,
        # 城市词/时效词占比此前只存不用，页面上却写着"下次 AI 选题生效"
        "city_rule": title_context.city_rule(count, getattr(cfg, "city_ratio", 60), cities),
        "genre_mix": (f"本次文体配比：{mix_text}；"
                      + title_context.timeliness_hint(getattr(cfg, "timeliness_ratio", 25))),
        "directions": getattr(cfg, "directions", None) or f"按品牌知识库里的核心服务与工艺展开（{business}）",
        "example_hint": "本地客户最容易踩的坑有哪些",
        "winners": "",
        "competitors": "",
        "existing": "\n".join(existing_list),
        # benefit_hints 已退役（v1.1 收口）：T 段正文从未引用该占位符，
        # 利益点表达由 business / directions 承载，见 spec/_共享/变量注册表.md。
    }
    prompt = title_context.inject(
        lib.render("T", variables, override=custom_text("T")),
        title_context.brand_block(brand_content, brand_main, brand_alias),
        title_context.audience_block((audience.content if audience else "") or ""),
    )
    # 时效硬要求（年份）：规格 T 段没有年份变量，模型也就从不写年份 ——
    # 实测存量标题「上海玉镯种水修复哪家能接？」一条都没有，GEO 抓不到新鲜度。
    prompt = f"{prompt}\n\n{title_context.year_block()}"
    raw = _llm(llm).complete([{"role": "user", "content": prompt}])
    # 真模型会给标题加编号/加粗包装；清洗后再查重入库。
    # GEO：问号之后的"做法建议/卖点"尾巴一律砍掉（模型会顽固沿用旧 SEO 句式）。
    cleaned_titles = [keep_question_only(clean_title_line(line))
                      for line in raw.splitlines()]
    # 年份 + 长度兜底：Prompt 已要求但模型会漏；确定性补在最前面
    # （疑问句问号后的尾巴会被 keep_question_only 砍掉，后缀年份留不住）。
    # normalize_title 同时压到 30 字内（平台把「标题过长」判低质）。
    titles = [title_context.normalize_title(t) for t in cleaned_titles if t]
    repo = TenantScopedRepository(Title)
    out: list[str] = []
    for text in titles:
        if len(out) >= count:
            break
        t = text[:255]
        if t in existing:
            continue
        repo.add(Title(text=t, source="ai", used=False))
        existing.add(t)
        out.append(t)
    return out


# ── 入队入口（业务只调 platform.jobs.enqueue，不直接碰线程池）─────────────────

@task("article.generate")
def article_generate_job(payload: dict) -> dict:
    return generate_article(
        title_text=payload.get("title"),
        contact_mode=payload.get("contact_mode", "revise"),
        angle_id=payload.get("angle"),
        base=payload.get("base"),
    )


@task("titles.generate")
def titles_generate_job(payload: dict) -> dict:
    return {"titles": generate_titles(int(payload.get("count", 5)))}


@task("article.pipeline")
def article_pipeline_job(payload: dict) -> dict:
    """A→B→C 全流程 + 审稿判「建议重写」时自动回 A 段重写。

    payload 带 dir 时跳过 A（断点续跑，不消耗新标题）。
    重写环的细节在 `writer/rewrite.run_with_rewrite`（A3 行数预算拆出）。
    """
    from app.services.writer.rewrite import run_with_rewrite  # 延迟：防循环导入

    return run_with_rewrite(payload)
