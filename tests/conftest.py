"""测试夹具：临时 SQLite + 双租户种子数据。

注意：**种子数据显式传 tenant_id**，因为此时没有租户上下文（before_flush 不介入），
这是唯一允许"无上下文写租户表"的场景。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db as _db
from app.models import AppUser, Brand, Tenant, Title, TitleConfig

T1, T2 = "t_alpha", "t_beta"


def _fast_hash(raw: str) -> str:
    """测试用廉价哈希：默认 scrypt 每轮 ~0.3s，40+ 个用例会被拖到 2 分钟以上。
    check_password_hash 兼容任意算法，不影响验证逻辑。"""
    return generate_password_hash(raw, method="pbkdf2:sha256:1000")
BRAND_ALPHA = (
    "阿尔法珠宝，是杭州本地深耕十二年的实体珠宝修复工坊，主营断镯修复与金缮修缮。\n"
    "以下简称「阿尔法」。\n"
    "【资质与认证】\n主体名称：阿尔法珠宝\n统一社会信用代码：91330100MA0000000X\n"
    "【核心产品与服务】\n断镯修复系列：适合断裂手镯，采用无痕填充工艺。\n"
    "【核心方法】\n微晶融合无痕修复，是指用同源矿物粉料填充裂隙并分段固化的修复工艺。\n"
)
BRAND_BETA = (
    "贝塔科技，是上海的一家技术服务公司，提供软件外包。\n"
    "【资质与认证】\n主体名称：贝塔科技\n"
)


@pytest.fixture()
def tmp_path():
    """遮蔽 pytest 内置 tmp_path（本机安全层适配，2026-10-14）。

    本机安全删除层会把 pytest 的 numbered 临时目录（pytest-of-user/pytest-N、
    以及 pytest-current 符号链接）标记为「不可信装入点」：
    `pytest_sessionfinish` 里的 `cleanup_dead_symlinks` 一遍历就抛
    WinError 448，且崩溃发生在终端摘要**之前** —— `.pytest-result.json`
    从不落盘，CI 门禁只能读到过期结果（tools/ci.py 的判读基础）。

    改用自建固定目录后，pytest 的临时目录工厂整体不初始化
    （`_basetemp` 为 None，sessionfinish 直接返回），扫描逻辑随之跳过；
    atexit 的 numbered-dir 清理也不再注册。残留目录若删不掉也无害
    （不在 pytest-of-user 下，不会波及后续运行）。
    """
    d = pathlib.Path(tempfile.mkdtemp(prefix="geo-pytest-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    db_file = (tmp_path / "test.db").as_posix()
    monkeypatch.setenv("GEO_ENV", "test")
    monkeypatch.setenv("GEO_DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("GEO_LLM_PROVIDER", "fake")
    monkeypatch.setenv("GEO_LLM_KEYS", "k-alpha,k-beta,k-gamma")

    application = create_app("test")
    application.config["DATA_ROOT"] = str(tmp_path / "data")

    with application.app_context():
        _db.create_all()
        _seed()
        _db.session.commit()
    yield application
    with application.app_context():
        _db.session.remove()
        _db.drop_all()


def _seed() -> None:
    _db.session.add_all([
        Tenant(id=T1, name="阿尔法珠宝", status="active"),
        Tenant(id=T2, name="贝塔科技", status="active"),
    ])
    _db.session.add_all([
        AppUser(tenant_id=T1, username="alpha", password_hash=_fast_hash("pw-alpha"),
                role="client", active=True),
        AppUser(tenant_id=T2, username="beta", password_hash=_fast_hash("pw-beta"),
                role="client", active=True),
        AppUser(tenant_id=T1, username="root", password_hash=_fast_hash("pw-root"),
                role="admin", active=True),
    ])
    _db.session.add_all([
        Brand(tenant_id=T1, content=BRAND_ALPHA),
        Brand(tenant_id=T2, content=BRAND_BETA),
        TitleConfig(tenant_id=T1, business="珠宝修复", cities="杭州, 上海",
                    expert="本地生活服务搜索营销专家", timeliness_ratio=80),
        Title(tenant_id=T1, text="杭州断镯修复哪家好？口碑榜前5家实测", source="ai"),
        Title(tenant_id=T1, text="玉镯修复多少钱？2026年杭州收费明细", source="ai"),
        Title(tenant_id=T2, text="上海软件开发公司怎么选", source="ai"),
    ])


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ── 结果落盘 ────────────────────────────────────────────────────────────────
# 本机安全删除层在 pytest 退出时抛 SystemExit（atexit 清理临时目录被拦），
# stdout 的汇总行会一起丢失。改为在终端摘要阶段落盘，让 CI 与人工都读得到权威数字。
# 注意：必须用 pytest_terminal_summary 而非 sessionfinish——后者触发时
# terminalreporter.stats 已被清空，会写出 total=0 的假结果。
_RESULT_FILE = pathlib.Path(__file__).resolve().parents[1] / ".pytest-result.json"


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:  # noqa: ARG001
    stats = terminalreporter.stats
    passed = [r.nodeid for r in stats.get("passed", []) if r.when == "call"]
    failed = [r.nodeid for r in stats.get("failed", []) if r.when == "call"]
    skipped = [r.nodeid for r in stats.get("skipped", [])]
    _RESULT_FILE.write_text(
        json.dumps(
            {
                "exitstatus": int(exitstatus),
                "total": len(passed) + len(failed) + len(skipped),
                "passed": len(passed),
                "failed": len(failed),
                "skipped": len(skipped),
                "failed_ids": failed,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
