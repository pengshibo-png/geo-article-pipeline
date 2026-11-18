"""config 蓝图 · 领域服务（业务规则/编排；不 import Flask request，不写裸 SQL）。

Phase 2 已实现：品牌资料（读写 + 完整度派生）、业务城市、画像。
Phase 3+ 填充：选题库相关。

**数据访问全部经TenantScopedRepository**（CI 静态扫描 V1 禁止裸查）。
完整度算法在 `app/services/brand_completeness.py`（S3 冻结规范的运行时版本）。
"""
from __future__ import annotations

from app.blueprints.config.profile_codec import deserialize_profile, serialize_profile
from app.errors import NotFound, ValidationError
from app.extensions import db as _db
from app.logging import get_logger
from app.models import Audience, Brand, City, TitleConfig
from app.services.brand_completeness import SECTION_NAMES, analyze
from app.tenancy import TenantScopedRepository, current_tenant

log = get_logger(__name__)

# 城市占比合计上限——原系统为百分比，合计不应超100
_RATIO_TOTAL_MAX = 100


class ConfigService:
    """品牌资料/城市/读者画像（业务事实源）"""

    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    # ── 品牌资料 ───────────────────────────────────────────────────────────

    def get_brand(self) -> dict:
        """读品牌资料 + 完整度派生。

        **一租户一份**（tenant_id 即主键），不存在返回空内容而非 404——
        与原系统 `/api/brand` 恒返回 `{content}` 的行为一致。
        """
        repo = TenantScopedRepository(Brand, self.session)
        row = repo.first()
        content = row.content if row else ""
        return {"content": content, **analyze(content)}

    def save_brand(self, content: str) -> dict:
        """整份覆盖保存。返回保存后的完整度快照。

        不做"分区级增量保存"——原系统是整份 textarea 提交，
        拆成增量反而会引入合并歧义（分区块是行首标记，不是可独立寻址的记录）。
        """
        if not isinstance(content, str):
            raise ValidationError("content 必须是字符串")
        if len(content) > 200_000:
            raise ValidationError("品牌资料过长（上限 20 万字）")

        repo = TenantScopedRepository(Brand, self.session)
        row = repo.first()
        if row is None:
            row = Brand(tenant_id=current_tenant(), content=content)
            repo.add(row)
        else:
            row.content = content
        self.session.commit()

        result = {"ok": True, "content": content, **analyze(content)}
        log.info(
            "品牌资料已保存",
            extra={
                "event": "brand_saved",
                "chars": result["total_chars"],
                "completeness": result["completeness"],
            },
        )
        return result

    def brand_blockers(self) -> list[dict]:
        """**阻塞生成的问题清单**（按后果分级，brand-names.md R2）。

        品牌名提取失败 / 主体名不一致 → 阻塞（对外可见的事故）。
        完整度不足 → 警告（自动推导，不阻塞）。
        """
        data = self.get_brand()
        names = data["derived"]["brand_names"]
        out: list[dict] = []
        if not names["confirmed"]:
            out.append({
                "level": "block",
                "code": "brand_name_missing",
                "message": "未能从「品牌概述」首句提取品牌名，请确认首句是否为「正式名称，是……」格式",
            })
        if names["entity_mismatch"]:
            out.append({
                "level": "block",
                "code": "entity_mismatch",
                "message": f"「资质与认证」的主体名称（{names['entity']}）与品牌名（{names['main']}）不一致",
            })
        if data["completeness"] < 100:
            out.append({
                "level": "warn",
                "code": "incomplete",
                "message": f"完整度{data['completeness']}%，未填分区：{'、'.join(data['missing'])}",
            })
        return out

    # ── 业务城市 ───────────────────────────────────────────────────────────

    def list_cities(self) -> list[dict]:
        """按占比降序、同名按id 升序，保证分页稳定。"""
        repo = TenantScopedRepository(City, self.session)
        rows = repo.query().order_by(City.ratio.desc(), City.id.asc()).all()
        return [
            {"id": r.id, "name": r.name, "ratio": r.ratio, "enabled": bool(r.enabled)}
            for r in rows
        ]

    def add_cities(self, names: list[str], ratio: int = 0) -> dict:
        """批量添加。**同名跳过而非报错**——批量导入场景下报错无意义。

        `skipped` 同时回报「批内重复」与「库中已存在」：调用方需要知道
        哪些请求没生效，静默丢弃会让人误以为全写进去了。
        """
        if not 0 <= ratio <= _RATIO_TOTAL_MAX:
            raise ValidationError(f"占比须在 0–{_RATIO_TOTAL_MAX} 之间")

        repo = TenantScopedRepository(City, self.session)
        existing = {r.name for r in repo.all()}
        added: list[str] = []
        skipped: list[str] = []
        for raw in names:
            v = str(raw or "").strip()
            if not v:
                continue
            if v in existing:
                skipped.append(v)
                continue
            repo.add(City(name=v, ratio=ratio, enabled=True))
            existing.add(v)
            added.append(v)
        if not added and not skipped:
            raise ValidationError("城市名不能为空")
        self.session.commit()
        log.info("城市批量添加", extra={"event": "cities_added", "added": len(added), "skipped": len(skipped)})
        return {"added": added, "skipped": skipped}

    def update_city(self, city_id: int, **fields) -> dict:
        """改占比/启停。**不允许改名**——城市名被 `{cities}` 变量引用，
        改名会让已生成的Prompt 变量与库内记录脱节，正确做法是删了重建。"""
        repo = TenantScopedRepository(City, self.session)
        row = repo.query().filter(City.id == city_id).first()
        if row is None:
            raise NotFound("城市不存在")

        if "name" in fields and fields["name"] != row.name:
            raise ValidationError("城市名不支持修改，请删除后重新添加")
        if "ratio" in fields:
            r = int(fields["ratio"])
            if not 0 <= r <= _RATIO_TOTAL_MAX:
                raise ValidationError(f"占比须在 0–{_RATIO_TOTAL_MAX} 之间")
            row.ratio = r
        if "enabled" in fields:
            row.enabled = bool(fields["enabled"])
        self.session.commit()
        return {"id": row.id, "name": row.name, "ratio": row.ratio, "enabled": bool(row.enabled)}

    def delete_city(self, city_id: int) -> None:
        repo = TenantScopedRepository(City, self.session)
        row = repo.query().filter(City.id == city_id).first()
        if row is None:
            raise NotFound("城市不存在")
        repo.delete(row)
        self.session.commit()

    # ── 业务城市 · 客户工作台（原站「业务城市」页契约）──────────────────────

    def get_city_config(self) -> dict:
        """双形状读：管理端用 `items`，客户页用 `cities/ratio/count`。

        `cities` 以 TitleConfig.cities 为准（与生成侧 `_city_context` 同源同拆法），
        而不是 City 表——两处只在 sync 后才保证一致，管理页逐条增删不回写标题配置。
        """
        repo = TenantScopedRepository(TitleConfig, self.session)
        cfg = repo.first()
        text = (cfg.cities or "") if cfg else ""
        names = [s.strip() for s in text.replace("，", ",").replace("\n", ",").split(",")]
        names = [s for s in names if s]
        ratio = int(cfg.city_ratio) if cfg else 60
        return {
            "items": self.list_cities(),
            "cities": names,
            "ratio": ratio,
            "count": len(names),
        }

    def sync_cities(self, names: list[str], ratio: int = 60) -> dict:
        """整份同步（原站客户页 POST {cities, ratio} → {ok, count}）。

        三件事一次做完：City 表差量重建（删多余/补缺失，存量行保留占比与启停）、
        TitleConfig.cities 逗号串回写（生成侧 {cities}/{city_rule} 的唯一输入）、
        city_ratio 落库。同名去重、空白跳过。
        """
        if not 0 <= int(ratio) <= _RATIO_TOTAL_MAX:
            raise ValidationError(f"城市词占比须在 0–{_RATIO_TOTAL_MAX} 之间")
        wanted: list[str] = []
        for raw in names:
            v = str(raw or "").strip()
            if v and v not in wanted:
                wanted.append(v)

        city_repo = TenantScopedRepository(City, self.session)
        existing = {r.name: r for r in city_repo.all()}
        for name, row in existing.items():
            if name not in wanted:
                city_repo.delete(row)
        for name in wanted:
            if name not in existing:
                city_repo.add(City(name=name, ratio=0, enabled=True))

        cfg_repo = TenantScopedRepository(TitleConfig, self.session)
        cfg = cfg_repo.first()
        if cfg is None:
            cfg = TitleConfig(tenant_id=current_tenant(), city_ratio=int(ratio))
            cfg_repo.add(cfg)
        cfg.cities = ", ".join(wanted)
        cfg.city_ratio = int(ratio)
        self.session.commit()
        log.info("业务城市整份同步", extra={"event": "cities_synced", "count": len(wanted), "ratio": ratio})
        return {"ok": True, "count": len(wanted)}

    # ── 读者画像 ───────────────────────────────────────────────────────────

    def get_audience(self) -> dict:
        repo = TenantScopedRepository(Audience, self.session)
        row = repo.first()
        return {"content": row.content if row else ""}

    def save_audience(self, content: str) -> dict:
        if not isinstance(content, str):
            raise ValidationError("content 必须是字符串")
        repo = TenantScopedRepository(Audience, self.session)
        row = repo.first()
        if row is None:
            repo.add(Audience(tenant_id=current_tenant(), content=content))
        else:
            row.content = content
        self.session.commit()
        return {"ok": True, "content": content}

    # ── 读者画像 · 结构化（原站「客户画像」页契约）────────────────────────

    def get_audience_profile(self) -> dict:
        """GET 形状：{content, profile, insight}。content 保持行式文本
        （生成侧 {audience} 变量的输入），profile 供表单回填。
        insight 由 /api/audience/analyze 生成后落盘（可再生的派生数据）。"""
        from app.services import audience_store

        repo = TenantScopedRepository(Audience, self.session)
        row = repo.first()
        content = row.content if row else ""
        return {"content": content, "profile": deserialize_profile(content),
                "insight": audience_store.load_insight()}

    def save_audience_profile(self, profile: dict, insight: str | None = None) -> dict:
        """POST {profile, insight?} → 序列化为行式文本整份覆盖。

        insight 为页面回传的当前洞察（AI 洞察接真后它才有值）；显式给出时才
        覆盖落盘，不给则保留盘上已有的（避免保存画像把洞察清掉）。
        """
        if not isinstance(profile, dict):
            raise ValidationError("profile 必须是对象")
        content = serialize_profile(profile)
        self.save_audience(content)
        if insight is not None:
            from app.services import audience_store

            audience_store.save_insight(insight)
        from app.services import audience_store

        return {"ok": True, "content": content,
                "profile": deserialize_profile(content),
                "insight": insight if insight is not None
                else audience_store.load_insight()}


__all__ = ["ConfigService", "SECTION_NAMES"]