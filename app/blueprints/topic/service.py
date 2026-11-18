"""topic 蓝图 · 领域服务（业务规则/编排；不 import Flask request，不写裸 SQL）。

B3 起实现「选题配置」读/存（原站 /api/my-title-config 契约）；
Phase 2+ 填充选题库 CRUD、AI 选题、同行抓取。
所有数据访问必须经 TenantScopedRepository。
"""
from __future__ import annotations

from app.errors import ValidationError
from app.extensions import db as _db
from app.logging import get_logger
from app.models import GenreMix, TitleConfig
from app.models.title import GENRES
from app.tenancy import TenantScopedRepository, current_tenant

log = get_logger(__name__)

# 原站样本的出厂配比（genre_mix 缺省时的兜底）
_DEFAULT_MIX: dict[str, int] = {"商业推荐": 80, "价格咨询": 10, "避坑对比": 5, "科普知识": 5}
_TIMELINESS_MAX = 100  # 页面滑杆自身封顶 80，服务端放宽到百分比上限
_TOPICS_MAX = 50


class TopicService:
    """选题库 CRUD、AI 选题、同行抓取、文体配比"""

    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    # ── 选题配置（原站 /api/my-title-config）───────────────────────────────

    def get_title_config(self) -> dict:
        """四文体配比 + 时效词占比 + 只读展示字段（业务/专家/城市/方向）。

        cities/directions 的拆法与生成侧一致（`_city_context` 按 [,\\n] 拆城市）。
        example_hint（避坑类问法示例）原站有回传但页面未消费，本版不落库、不回传。
        """
        cfg_repo = TenantScopedRepository(TitleConfig, self.session)
        cfg = cfg_repo.first()
        mix_repo = TenantScopedRepository(GenreMix, self.session)
        stored = {r.genre: int(r.ratio) for r in mix_repo.all()}
        mix = {g: stored.get(g, _DEFAULT_MIX.get(g, 0)) for g in GENRES}

        cities = _split_cities(cfg.cities if cfg else "")
        topics = _split_multi(cfg.directions if cfg else "")
        return {
            "business": (cfg.business or "") if cfg else "",
            "expert": (cfg.expert or "") if cfg else "",
            "cities": cities,
            "city_ratio": int(cfg.city_ratio) if cfg else 60,
            "topics": topics,
            "genre_mix": mix,
            "timeliness_ratio": int(float(cfg.timeliness_ratio)) if cfg else 25,
        }

    def save_title_config(self, body: dict) -> dict:
        """页面只提交 genre_mix + timeliness_ratio；business/expert 等由「业务城市」页
        的 sync 接口维护，这里仅当显式传入时才写（宽容超集，不回清既有值）。"""
        if not isinstance(body, dict):
            raise ValidationError("请求体必须是 JSON 对象")

        mix = _normalize_mix(body.get("genre_mix"))
        timeliness = body.get("timeliness_ratio")
        if timeliness is not None:
            try:
                timeliness = max(0, min(_TIMELINESS_MAX, int(timeliness)))
            except (TypeError, ValueError):
                raise ValidationError("timeliness_ratio 必须是整数") from None

        cfg_repo = TenantScopedRepository(TitleConfig, self.session)
        cfg = cfg_repo.first()
        if cfg is None:
            cfg = TitleConfig(tenant_id=current_tenant())
            cfg_repo.add(cfg)
        if "business" in body:
            cfg.business = str(body["business"] or "")[:255]
        if "expert" in body:
            cfg.expert = str(body["expert"] or "")[:128]
        if "directions" in body:
            cfg.directions = str(body["directions"] or "")
        if timeliness is not None:
            cfg.timeliness_ratio = timeliness

        mix_repo = TenantScopedRepository(GenreMix, self.session)
        rows = {r.genre: r for r in mix_repo.all()}
        for genre, ratio in mix.items():
            row = rows.get(genre)
            if row is None:
                mix_repo.add(GenreMix(tenant_id=current_tenant(), genre=genre, ratio=ratio))
            else:
                row.ratio = ratio
        self.session.commit()

        saved_mix = {g: int(v) for g, v in mix.items()}
        log.info("文体配比已保存", extra={"event": "genre_mix_saved", "mix": saved_mix})
        return {
            "ok": True,
            "genre_mix": saved_mix,
            "timeliness_ratio": int(float(cfg.timeliness_ratio)),
            "msg": "已保存",
        }


def _normalize_mix(raw) -> dict[str, int]:
    """四文体归一到合计 100 的整数。全 0/缺省回落出厂配比；未知键忽略。"""
    values: dict[str, int] = {}
    for genre in GENRES:
        try:
            values[genre] = max(0, int((raw or {}).get(genre, 0)))
        except (TypeError, ValueError):
            raise ValidationError(f"genre_mix.{genre} 必须是整数") from None
    total = sum(values.values())
    if total <= 0:
        return dict(_DEFAULT_MIX)
    scaled = {g: round(v * 100 / total) for g, v in values.items()}
    # 取整误差归到最大项，保证合计恒 100（页面 gmBalance 同款兜底）
    drift = 100 - sum(scaled.values())
    top = max(scaled, key=lambda g: scaled[g])
    scaled[top] = max(0, scaled[top] + drift)
    return scaled


def _split_cities(text: str | None) -> list[str]:
    """城市串 → 列表。拆法与生成侧 `_city_context` 一致（仅 [,\\n]，顿号不是分隔符）。"""
    if not text:
        return []
    out: list[str] = []
    for chunk in text.replace("，", ",").replace("\n", ",").split(","):
        v = chunk.strip()
        if v and v not in out:
            out.append(v)
    return out


def _split_multi(text: str | None) -> list[str]:
    """自由文本 → 有序去重列表（顿号/分号/逗号/换行均可作分隔）。"""
    if not text:
        return []
    out: list[str] = []
    for chunk in text.replace("，", ",").replace("；", ";").replace("、", ",").replace("\n", ",").split(","):
        v = chunk.strip()
        if v and v not in out:
            out.append(v)
    return out[:_TOPICS_MAX]


__all__ = ["TopicService"]
