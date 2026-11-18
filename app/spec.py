"""S3 冻结规格的定位入口。

四段 Prompt、变量注册表、{contact_rule}、{brand_names} 的唯一来源都在 S3 目录，
**代码只引用、不复制**（门禁 G10）。路径可用环境变量 GEO_SPEC_DIR 覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

from app.config import PROJECT_ROOT


def spec_dir() -> Path:
    env = os.environ.get("GEO_SPEC_DIR")
    if env:
        return Path(env)
    return PROJECT_ROOT / "spec"


def prompts_dir() -> Path:
    return spec_dir() / "prompts"


def shared_dir() -> Path:
    return prompts_dir() / "_共享"


def prompts_files() -> dict[str, str]:
    return {
        "T": str(prompts_dir() / "T-标题选题.md"),
        "A": str(prompts_dir() / "A-写文章.md"),
        "B": str(prompts_dir() / "B-审稿质检.md"),
        "C": str(prompts_dir() / "C-修改文章.md"),
    }


def variable_registry() -> Path:
    return shared_dir() / "variable-registry.json"


def contact_rule_file() -> Path:
    return shared_dir() / "contact-rule.md"


def legacy_citable_facts_file() -> Path:
    """事实库的**旧**位置（共享目录）。只用于搬迁提示，**不再被读取**。

    留着它是为了在租户文件缺失时能明确告诉运维"东西还在老地方、要搬"，
    而不是让功能悄无声息地消失。
    """
    return shared_dir() / "citable-facts.md"


def citable_facts_file() -> Path:
    """{可引用事实库}：文章**唯一允许引用**的外部事实来源（**按租户隔离**）。

    收录有公开权威来源的事实（GB/T 标准、检测体系、工艺分类），并显式列出
    "不可引用"与"待品牌方补充"两类 —— 模型编造资质的根因是"没料可写"，
    光靠提示词禁止不够，得给它一份能引用的清单。

    位置：`<DATA_ROOT>/<租户 ID>/spec/citable-facts.md`（与 douyin / audience
    等子系统同一约定）。

    为什么从 `_共享/` 搬走：这份文件被**整段**拼进 A 段 Prompt，内容天然带行业
    属性（实机里是玉石/珠宝修复的国标与工艺分类）。挂在进程级共享目录上 =
    一个路径服务所有租户 → 多租户部署下租户 A 的行业事实会写进租户 B 的文章，
    属于跨租户数据泄漏。规格里「只引用、不复制」（G10）约束的是**模板**；
    事实库是**租户数据**，必须跟租户走。
    """
    from flask import current_app

    from app.tenancy import current_tenant

    return Path(current_app.config["DATA_ROOT"]) / current_tenant() / "spec" / "citable-facts.md"


def brand_names_file() -> Path:
    return shared_dir() / "brand-names.md"


def brand_sections_dir() -> Path:
    """品牌资料九大分区规范目录。"""
    return spec_dir() / "品牌资料规范"


def brand_sections_file() -> Path:
    """九大分区机读规格（分区名/门槛/必含要素）。

    `order_frozen=true` —— 分区名与顺序是逐字契约。
    """
    return brand_sections_dir() / "brand-sections.json"
