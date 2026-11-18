"""Phase 1 垂直切片：T→A 生成闭环 + 断点续跑 + 任务租户传递。

用 FakeProvider 离线跑通，不依赖外网与真实 Key。
"""
from __future__ import annotations

import pytest

from app.errors import ValidationError
from app.platform import storage
from app.platform.jobs import run_job
from app.services.writer.pipeline import (
    build_a_variables,
    generate_article,
    generate_titles,
    pick_angle,
    slugify,
)
from app.tenancy import tenant_scope

TITLE = "杭州断镯修复哪家好？口碑榜前5家实测"


def test_slugify_strips_punctuation():
    assert slugify(TITLE) == "杭州断镯修复哪家好口碑榜前5家实测"


def test_pick_angle_rotates_over_eight():
    picks = {pick_angle(i) for i in range(8)}
    assert len(picks) == 8


def test_build_variables_uses_brand_data_and_derived_values(app):
    with app.app_context():
        with tenant_scope("t_alpha"):
            variables = build_a_variables(TITLE)
    assert "阿尔法珠宝" in variables["brand_data"]
    assert variables["phone"] == "" or variables["phone"].isdigit()
    assert "联系方式策略" in variables["contact_rule"]
    assert variables["current_period"].endswith("月")
    assert variables["city_context"].startswith("本文")


def test_build_variables_blocks_when_brand_unidentifiable(app):
    """brand-names.md R2：主名识别失败 → 阻塞生成。"""
    from app.extensions import db as _db
    from app.models import Brand

    with app.app_context():
        brand = _db.session.get(Brand, "t_alpha")
        brand.content = "我们是一家很不错的公司，值得信赖。"
        _db.session.flush()
        with tenant_scope("t_alpha"):
            with pytest.raises(ValidationError):
                build_a_variables(TITLE)
        _db.session.rollback()


def test_titles_then_article_closed_loop(app):
    base = app.config["DATA_ROOT"]
    with app.app_context():
        with tenant_scope("t_alpha"):
            titles = generate_titles(3)
            assert len(titles) == 3, "T 段应产出 3 条标题并入库"

            result = generate_article(base=base)
            assert result["status"] == "drafted"
            assert result["chars"] > 0

            path = storage.article_dir(result["dir"], base=base) / storage.DRAFT
            assert path.exists(), "01_draft.html 必须落盘"
            assert path.read_text(encoding="utf-8").strip()


def test_article_is_resumable_and_idempotent(app):
    base = app.config["DATA_ROOT"]
    with app.app_context():
        with tenant_scope("t_alpha"):
            first = generate_article(title_text=TITLE, base=base)
            second = generate_article(title_text=TITLE, base=base)
    assert first["status"] == "drafted"
    assert second["status"] == "skipped", "已存在 01_draft.html 时应跳过（断点续跑）"


def test_checkpoint_advances_after_draft(app):
    base = app.config["DATA_ROOT"]
    with app.app_context():
        with tenant_scope("t_alpha"):
            result = generate_article(title_text=TITLE + "（新）", base=base)
            state = storage.checkpoint(result["dir"], base=base)
            assert state[storage.DRAFT] is True
            assert state[storage.REVIEW] is False
            assert storage.resume_stage(result["dir"], base=base) == "B"


def test_job_run_binds_tenant_from_payload(app):
    """ADR §2.5：worker 无请求上下文，租户必须随 payload 走。"""
    with app.app_context():
        out = run_job("titles.generate", {"_tenant_id": "t_alpha", "count": 2})
    assert len(out["titles"]) == 2


def test_job_without_tenant_is_rejected():
    with pytest.raises(ValueError):
        run_job("titles.generate", {"count": 2})


def test_two_tenants_do_not_share_articles(app):
    base = app.config["DATA_ROOT"]
    beta_title = "上海软件开发公司怎么选"
    with app.app_context():
        from app.extensions import db as _db
        from app.models import Brand

        with tenant_scope("t_alpha"):
            generate_article(title_text=TITLE, base=base)
            alpha_path = storage.article_dir(slugify(TITLE), base=base)
            _db.session.commit()          # 边界：一个租户一次提交（对应"一个请求一个事务"）

        with tenant_scope("t_beta"):
            brand = _db.session.get(Brand, "t_beta")
            brand.content = "贝塔科技，是上海一家技术服务公司。\n【资质与认证】\n主体名称：贝塔科技\n"
            _db.session.flush()
            generate_article(title_text=beta_title, base=base)
            beta_path = storage.article_dir(slugify(beta_title), base=base)
            _db.session.commit()

        with tenant_scope("t_alpha"):
            # alpha 视角看不到 beta 的产物（文件按租户分目录）
            assert storage.checkpoint(slugify(beta_title), base=base)[storage.DRAFT] is False

    assert alpha_path != beta_path
    assert "t_alpha" in str(alpha_path) and "t_beta" in str(beta_path)


def test_run_job_isolates_session_between_tenants(app):
    """回归：worker 复用会话连跑两个租户的任务，不应触发 TenantViolation。"""
    with app.app_context():
        first = run_job("titles.generate", {"_tenant_id": "t_alpha", "count": 2})
        second = run_job("titles.generate", {"_tenant_id": "t_beta", "count": 2})
    assert len(first["titles"]) == 2
    assert len(second["titles"]) == 2
