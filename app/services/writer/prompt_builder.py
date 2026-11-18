"""四段 Prompt 渲染器：读 S3 冻结的 .md，剥离注释，按变量注入。

- 变量清单**从 Prompt 正文解析**得到（不手工维护第二份）。
- `strict=True` 时缺必填变量直接报错 —— 对应"只注入规则不注入号码，模型无法判断"那类坑。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.errors import ConfigError
from app.logging import get_logger
from app.spec import prompts_files

log = get_logger(__name__)

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_VAR = re.compile(r"\{([a-z][a-z0-9_]*)\}")


class PromptLibrary:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        files = files or prompts_files()
        self._text: dict[str, str] = {}
        for step, path in files.items():
            p = Path(path)
            if not p.exists():
                raise ConfigError(f"缺少 {step} 段 Prompt 文件：{p}")
            self._text[step] = _COMMENT.sub("", p.read_text(encoding="utf-8")).strip()
        self._required = {s: sorted(set(_VAR.findall(t))) for s, t in self._text.items()}

    def required(self, step: str) -> list[str]:
        if step not in self._text:
            raise ConfigError(f"未知的 Prompt 段：{step}（可选 {sorted(self._text)}）")
        return list(self._required[step])

    def raw(self, step: str) -> str:
        """指定段的默认正文（剥注释后），供「AI 指令自定义」页回显默认模板。"""
        if step not in self._text:
            raise ConfigError(f"未知的 Prompt 段：{step}（可选 {sorted(self._text)}）")
        return self._text[step]

    def render(self, step: str, variables: dict, *, strict: bool = True,
               override: str | None = None) -> str:
        """渲染指定段。override=租户自定义全文（「AI指令自定义」页保存的内容）时，
        **用自定义文本替代冻结模板**，变量注入规则不变。

        override 分支不做必填校验（自定义文本本来就未必引用全部变量），但**未知
        占位符会记一条日志**：`{bran_data}` 这类笔误不会被替换，原样发给模型，
        模型只能当普通文字看 —— 保存接口已拦，这里兜住存量数据与直接写库的调用方。
        """
        if override is not None:
            text = _COMMENT.sub("", override).strip()
            unknown = sorted(set(_VAR.findall(text)) - set(self._required.get(step, ())))
            if unknown:
                log.warning("custom_prompt_unknown_vars", extra={
                    "event": "custom_prompt_unknown_vars", "step": step,
                    "vars": unknown, "hint": "这些占位符不会被替换，将原样发给模型"})
            return _VAR.sub(lambda m: str(variables.get(m.group(1), m.group(0))), text)
        missing = [name for name in self.required(step) if name not in variables]
        if strict and missing:
            raise ConfigError(f"{step} 段缺少必填变量：{missing}")
        return _VAR.sub(lambda m: str(variables.get(m.group(1), m.group(0))), self._text[step])


_legacy_facts_warned = False


def _warn_legacy_facts_once(legacy: Path) -> None:
    """租户事实库缺失、而老共享目录里还躺着一份 → 提示搬迁，**一次**，且不读它。

    为什么只提示不回落：回落到老文件就是"某租户没配就直接用别人的事实"，
    正是这次要消除的跨租户泄漏；而一句不提示又会让"可引用事实库"这个特性
    无声失效（生成照常成功，文章里却再也没有可引用清单）。
    """
    global _legacy_facts_warned
    if _legacy_facts_warned or not legacy.exists():
        return
    _legacy_facts_warned = True
    log.warning("citable_facts_not_migrated", extra={
        "event": "citable_facts_not_migrated", "legacy": str(legacy),
        "hint": "事实库已按租户隔离，请把该文件移到当前租户的 spec/citable-facts.md"})


def citable_facts() -> str:
    """读**当前租户**的事实库（剥掉 HTML 注释头）。每次都读盘：改完不用重启。

    路径见 `spec.citable_facts_file()`：`<DATA_ROOT>/<租户>/spec/citable-facts.md`。
    文件缺失返回空串 —— 事实库是增强项，不该阻塞生成。
    """
    from app.spec import citable_facts_file, legacy_citable_facts_file

    try:
        text = citable_facts_file().read_text(encoding="utf-8")
    except OSError:
        _warn_legacy_facts_once(legacy_citable_facts_file())
        return ""
    return _COMMENT.sub("", text).strip()


def custom_text(step: str) -> str | None:
    """当前租户的自定义指令文本；未自定义返回 None（调用方回落冻结模板）。

    services 层直接读 Prompt 表（V1 裸查扫描只针对 blueprints）；
    与 config 蓝图 PromptService 写的是同一张表，页面保存即生效。
    """
    from app.models import Prompt
    from app.tenancy import TenantScopedRepository

    row = TenantScopedRepository(Prompt).first(step=step)
    if row is not None and row.is_custom and (row.content or "").strip():
        return row.content
    return None
