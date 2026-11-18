"""四段指令（T/A/B/C）的租户级自定义（原站「AI 指令自定义」页契约）。

形状对齐 参考系统接口实测：
`{prompts: [{step, name, content, desc, is_custom, length}, ...]}`，顺序 A/B/C/T。

- `content`：租户自定义文本；未自定义时回显 S3 冻结默认（PromptLibrary.raw）。
- `is_custom`：Prompt 表有行**且内容非空**才为 True（与生成侧 `custom_text` 同一判据）；
  「恢复默认」删行回落默认。
- `vars`：各段真实可用变量（从 Prompt 正文解析，见 `PromptService.variables`）。

生成侧已接线（Phase III）：writer 流水线渲染 T/A/B/C 前经
`prompt_builder.custom_text(step)` 查本租户覆盖，保存后下一批立即生效；
重置（删行）即回落冻结模板。注意 B 段被改写后仍需产出能通过
`validate_review` 冻结契约的 JSON，否则审稿报错——页面上这是用户的编辑后果。
"""
from __future__ import annotations

import re

from app.errors import ConfigError, NotFound, ValidationError
from app.extensions import db as _db
from app.logging import get_logger
from app.models import Prompt
from app.services.writer.prompt_builder import PromptLibrary
from app.spec import prompts_files
from app.tenancy import TenantScopedRepository, current_tenant

log = get_logger(__name__)

# (名称, 描述)——与原站样本逐字一致；顺序即响应顺序
STEP_META: dict[str, tuple[str, str]] = {
    "A": ("生成器", "AI写文章的核心指令——行业身份/写作规则/排版/防重复"),
    "B": ("审稿官", "AI质检审核的标准——七个维度的检查规则和评分标准"),
    "C": ("修改器", "AI根据审稿意见修改文章的原则和约束"),
    "T": ("标题选题", "AI生成文章标题/选题关键词的指令——标题结构/城市规则/主题方向"),
}
_MAX_CONTENT = 100_000

# 变量说明：`{name}` → 人话。页面的「可用变量」按钮由它渲染，
# **变量名本身从 Prompt 正文解析**（PromptLibrary.required），不在这里维护第二份
# 名单 —— 名单会漂移，正文不会。这里只补描述。
VAR_DESC: dict[str, str] = {
    "title": "文章标题（选题库分配的）",
    "brand_data": "品牌资料（品牌知识库的内容）",
    "audience": "读者画像（客户画像页内容 + AI 洞察）",
    "city_context": "城市定位（标题含哪个城市就锚定哪个）",
    "angle": "写作视角（8 视角之一，系统自动轮换）",
    "image_html": "配图 HTML（部分视角不配图则为空）",
    "phone": "品牌电话",
    "contact_rule": "联系方式处理规则（按 contact_mode 渲染）",
    "business_skeleton": "正文小节骨架（按品牌库核心服务推导）",
    "current_period": "当前时段（时效信号，如 2026年10月）",
    "business": "业务定位",
    "article": "待审 / 待改文章正文",
    "review": "审稿报告（B 段产出的 JSON 问题清单）",
    "brand_names": "该租户允许的品牌名与别名",
    "expert": "专家身份",
    "count": "本次要生成的标题数量",
    "cities": "业务城市列表（城市页配置的，/ 分隔）",
    "city_rule": "城市选题规则（由城市词占比算出带城/泛地域条数）",
    "example_hint": "避坑类标题的问法示例",
    "genre_mix": "四文体配比说明（含时效词占比）",
    "directions": "本次轮换的选题方向",
    "winners": "GEO 表现好的主题（来自监测数据）",
    "competitors": "同行标题训练材料（AI 引擎引用过的同行内容）",
    "existing": "已用过的标题（防重复，最近 120 条）",
}

# 变量占位符：与 prompt_builder 的 `_VAR` 同形（小写开头的标识符），
# 因此 JSON 示例里的 `{"overall_score": ...}`、中文 `{价格}` 都不算变量。
_VAR = re.compile(r"\{([a-z][a-z0-9_]*)\}")

# 默认正文懒加载缓存——S3 规范缺失时降级为空串（桌面精简包可能不带规范目录），
# 页面仍可编辑保存自定义内容，只是「恢复默认」无默认可回。
_lib: PromptLibrary | None = None
_lib_failed = False


class PromptService:
    """my-prompts 读/存/重置。**生成生效性见模块 docstring。**"""

    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    def list_prompts(self) -> dict:
        repo = TenantScopedRepository(Prompt, self.session)
        rows = {r.step: r for r in repo.all()}
        out = []
        for step, (name, desc) in STEP_META.items():
            row = rows.get(step)
            # 「已自定义」必须与生成侧**同一条判据**（prompt_builder.custom_text：
            # is_custom 且内容非空）。原来只看 is_custom，于是保存过空内容的租户
            # 会看到"已自定义"徽标，实际跑的还是内置模板 —— 页面在骗人。
            custom = bool(row and row.is_custom and (row.content or "").strip())
            content = row.content if custom else _default(step)
            out.append({
                "step": step,
                "name": (row.name if row else None) or name,
                "content": content,
                "desc": desc,
                "is_custom": custom,
                "length": len(content),
            })
        return {"prompts": out, "vars": self.variables()}

    def variables(self) -> dict[str, list[dict]]:
        """各段真实可用变量（从 Prompt 正文解析）+ 中文说明，供页面渲染变量按钮。

        页面上原来硬编码着一份名单，A 段只列了 6 个（正文实际用 11 个）、
        B 段只列了 1 个（实际 6 个）、C 段 3 个（实际 8 个）—— 用户照着名单写
        自定义指令，用到真实存在的变量也会被漏掉，反之写了不存在的也没人拦。
        """
        lib = _library()
        if lib is None:
            return {}
        out: dict[str, list[dict]] = {}
        for step in STEP_META:
            try:
                names = lib.required(step)
            except ConfigError:
                continue
            out[step] = [{"name": n, "desc": VAR_DESC.get(n, "")} for n in names]
        return out

    def save_prompt(self, step: str, content: str) -> dict:
        if step not in STEP_META:
            raise NotFound(f"未知指令段：{step}（可选 {sorted(STEP_META)}）")
        if not isinstance(content, str):
            raise ValidationError("content 必须是字符串")
        if len(content) > _MAX_CONTENT:
            raise ValidationError(f"指令过长（上限 {_MAX_CONTENT // 10000} 万字）")
        if not content.strip():
            # 空内容曾经被当成"自定义"存下去，实际生成侧回落内置 —— 语义相反。
            raise ValidationError("指令内容不能为空；要恢复内置模板请点「恢复默认」")

        unknown = self._unknown_vars(step, content)
        if unknown:
            known = ", ".join("{" + n + "}" for n in self._known_vars(step)) or "（无）"
            raise ValidationError(
                f"指令里有本段不存在的变量：{', '.join('{' + n + '}' for n in unknown)}。"
                f"这些占位符不会被替换，会原样发给模型。{step} 段可用变量：{known}")

        repo = TenantScopedRepository(Prompt, self.session)
        row = repo.query().filter(Prompt.step == step).first()
        if row is None:
            row = Prompt(tenant_id=current_tenant(), step=step, name=STEP_META[step][0])
            repo.add(row)
        row.name = STEP_META[step][0]
        row.content = content
        row.is_custom = True
        self.session.commit()
        log.info("指令已自定义", extra={"event": "prompt_saved", "step": step, "length": len(content)})
        out = {"ok": True, "length": len(content)}
        warning = _json_contract_warning(step, content)
        if warning:
            out["warning"] = warning
        return out

    def reset_prompt(self, step: str) -> dict:
        if step not in STEP_META:
            raise NotFound(f"未知指令段：{step}（可选 {sorted(STEP_META)}）")
        repo = TenantScopedRepository(Prompt, self.session)
        row = repo.query().filter(Prompt.step == step).first()
        if row is not None:
            repo.delete(row)
            self.session.commit()
        return {"ok": True}

    def _known_vars(self, step: str) -> list[str]:
        lib = _library()
        if lib is None:
            return []
        try:
            return lib.required(step)
        except ConfigError:
            return []

    def _unknown_vars(self, step: str, content: str) -> list[str]:
        known = set(self._known_vars(step))
        if not known:                     # 规范目录缺失 → 降级放行（见 _default 注释）
            return []
        return sorted(set(_VAR.findall(content)) - known)


def _json_contract_warning(step: str, content: str) -> str:
    """B 段自定义的两个坑，保存时如实提示（不拦，改不改是用户的编辑后果）。

    1. B 段的输出被程序当 JSON 解析（`validate_review` 冻结契约）。指令里不提
       JSON，模型很容易回一篇散文，审稿直接报错；
    2. 系统会自动在 B 段 Prompt 后面追加【输出硬性要求】输出契约 —— 用户写的
       "只输出纯文本"这类硬性要求会与之冲突。
    """
    if step != "B":
        return ""
    if "json" in content.lower():
        return ""
    return ("B 段（审稿官）的输出必须是 JSON：系统会在指令后自动追加输出契约，"
            "但建议指令本身也明确要求输出 JSON 字段，否则模型可能回一篇散文，审稿会直接失败。")


def _library() -> PromptLibrary | None:
    global _lib, _lib_failed
    if _lib is None and not _lib_failed:
        try:
            _lib = PromptLibrary(prompts_files())
        except Exception:  # noqa: BLE001 - 规范目录缺失时降级（见 _lib 注释）
            log.warning("S3 默认指令不可用，回显为空", exc_info=True)
            _lib_failed = True
    return _lib


def _default(step: str) -> str:
    lib = _library()
    if lib is None:
        return ""
    try:
        return lib.raw(step)
    except ConfigError:
        return ""


__all__ = ["PromptService", "STEP_META", "VAR_DESC"]
