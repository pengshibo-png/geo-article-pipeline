"""工程门禁（ADR §1.5 行数预算 / §1.3 依赖方向 / 20 条业务规则 A4·A11）。

这些是**结构性约束**：它们红了，说明代码组织在退化，而不是某个功能坏了。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def _lines(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


# ── A1 行数预算 ─────────────────────────────────────────────────────────────
def test_a1_app_py_at_most_60_lines():
    assert _lines(ROOT / "app.py") <= 60, "app.py 超过 60 行 —— 入口只应装配"


def test_a1_create_app_at_most_120_lines():
    lines = (APP / "__init__.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("def create_app"))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace():
            end = i
            break
    assert end - start <= 120, f"create_app 有 {end - start} 行（上限 120）"


# ── A3 单文件 300 行 ────────────────────────────────────────────────────────
# 开源基线说明：以下 6 个文件在开源化之前的开发中就超过了 300 行预算（存量技术债），
# 此处**冻结在各自当前行数**——只禁止继续增长，其余文件一律 300 行红线。
# 收敛它们是正常的后续重构 PR（按域拆文件即可，行为不变、测试兜底）。
_KNOWN_OVER_300 = {
    "app/services/douyin_pack.py": 302,
    "app/services/douyin_poi_build.py": 327,
    "app/services/writer/compliance.py": 509,
    "app/services/writer/pipeline.py": 377,
    "app/services/writer/review.py": 336,
    "app/blueprints/generation/douyin_jobs.py": 331,
}


def test_a3_no_file_over_300_lines():
    offenders = []
    for p in APP.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        n = _lines(p)
        budget = _KNOWN_OVER_300.get(rel, 300)
        if n > budget:
            offenders.append((rel, n, budget))
    assert not offenders, f"以下文件超过行数预算（存量冻结值见 _KNOWN_OVER_300）：{offenders}"


# ── A2 依赖方向（铁律）──────────────────────────────────────────────────────
def test_a2_services_never_import_blueprints():
    bad = [str(p.relative_to(ROOT)) for p in (APP / "services").rglob("*.py")
           if "app.blueprints" in p.read_text(encoding="utf-8")]
    assert not bad, f"services 不得依赖 blueprints：{bad}"


def test_a2_platform_never_imports_services():
    bad = [str(p.relative_to(ROOT)) for p in (APP / "platform").rglob("*.py")
           if "app.services" in p.read_text(encoding="utf-8")]
    assert not bad, f"platform 不得依赖 services：{bad}"


def test_a2_platform_never_imports_blueprints():
    bad = [str(p.relative_to(ROOT)) for p in (APP / "platform").rglob("*.py")
           if "app.blueprints" in p.read_text(encoding="utf-8")]
    assert not bad, f"platform 不得依赖 blueprints：{bad}"


# ── A11 业务代码不得直接碰线程池 ────────────────────────────────────────────
def test_a11_business_code_does_not_use_threadpool_directly():
    bad = []
    for base in (APP / "blueprints", APP / "services"):
        for path in base.rglob("*.py"):
            if "ThreadPoolExecutor" in path.read_text(encoding="utf-8"):
                bad.append(str(path.relative_to(ROOT)))
    assert not bad, f"业务代码必须经 platform/jobs.py 入队，不得直用线程池：{bad}"


# ── A4 租户身份只能来自会话 ─────────────────────────────────────────────────
def test_a4_tenant_binding_reads_only_session():
    body = (APP / "tenancy.py").read_text(encoding="utf-8").split("def init_tenancy", 1)[1]
    assert "request.args" not in body, "租户不得来自 query 参数"
    assert "request.get_json" not in body, "租户不得来自请求体"
    assert "request.form" not in body, "租户不得来自表单"
    assert 'session.get("view_tenant")' in body and 'session.get("uid")' in body


def test_a4_no_tenant_id_in_url_paths():
    """租户不得作为**路径变量**出现在路由里（admin 切租户走 session + POST body）。

    注意：`/api/admin/view-tenant` 这类**字面端点名**是合法的（管理员显式指定目标租户），
    被禁止的是 `/<tenant_id>` 这种可篡改的路径参数。
    """
    offenders = []
    pattern = re.compile(r'@bp\.\w+\("[^"]*<[^">]*tenant[^">]*>')
    for path in (APP / "blueprints").rglob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    assert not offenders, f"URL 中不得携带租户路径变量：{offenders}"
