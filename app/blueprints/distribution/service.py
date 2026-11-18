"""distribution 蓝图 · 领域服务（文章档案读取，archives.html 对接面）。

页面契约（参考系统线上行为实测）：
- GET /api/articles?limit=2000 → {articles:[{dir,files,published,created,publish_entries}], total}
- GET /api/article/<dir>/<filename> → {name, content}（文件名白名单 = storage.ASSET_FILES）

`files` 只含磁盘上真实存在的阶段产物（键=文件名，值=字节数）——
页面用 `'03_final.html' in files` 判断终稿就绪，全键返回会谎报。
发布动作（publish-pick/publish-one）是 Phase III 执行器，本期如实返回未开放；
渠道列表 `/api/client/channels` 是**纯数据组装**（账号额度都在库），本期接真。

ADR-011（虚拟聚合渠道）：`{code}-all`（平台聚合）与 `tenant-all`（全租户聚合）
只活在 API 响应里，**绝不入库**；发布解析必须经 `resolve_real_account()`，
虚拟项一律拒绝（否则会"选到虚拟账号"）。渠道形状冻结在「发布渠道」设计说明。
"""
from __future__ import annotations

from app.errors import NotFound, ValidationError
from app.extensions import db as _db
from app.models import Account, Article, PublishLog
from app.platform import storage
from app.timeutil import hm_string, natural_day_string
from app.tenancy import TenantScopedRepository

_MAX_LIMIT = 2000

# 平台 → (聚合渠道 ID 短码, 中文名)。meipian→mp 是源系统实测（S4 报告 ADR-011 行）。
PLATFORM_CODE = {"meipian": "mp", "souhu": "sh", "baijiahao": "bjh", "lieju": "lj", "wuba": "wu"}
PLATFORM_CN = {"meipian": "美篇", "souhu": "搜狐", "baijiahao": "百家号",
               "lieju": "列表网", "wuba": "58同城"}


def article_platforms() -> frozenset:
    """能承接**文章型**发布的平台集合（本机驱动 ∪ 蚁小二云分发）。

    - 本机 Playwright 适配器：美篇 / 搜狐 / 百家号 / 列表网 / 58同城
    - 蚁小二文章型平台：搜狐号 / 百家号 / 头条号 / 网易号 / 知乎 / 微信公众号 …

    抖音图文（douyin_imgtext）等图文/视频平台**不在其中**：它们各自有产线，
    文章驱动对它们必然报"平台不在文章发布支持范围"。所以文章渠道列表、
    "全部自有账号"聚合与智能分配都不该带上它们。
    """
    from app.services.publish.platforms import ADAPTERS
    from app.services.publish.yxer import ARTICLE_PLATFORMS

    return frozenset(ARTICLE_PLATFORMS | set(ADAPTERS))


class DistributionService:
    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    # ── 发布渠道（ADR-011：数据组装在响应期，虚拟项不入库）──────────────────

    @staticmethod
    def build_channels(accounts) -> list[dict]:
        """账号列表 → 渠道列表。

        只列**文章型平台**（`article_platforms()`）：抖音图文等图文/视频平台
        与文章驱动不兼容，展示了用户一点就报错，所以直接不出现。
        登记型（daily_limit=0）不参与发布（A23）；
        同平台 ≥2 个可发布账号才有平台聚合项（单账号聚合无意义）；
        聚合项 left 恒"不限"（源系统实测形状）。
        """
        out: list = []
        by_platform: dict = {}
        allowed = article_platforms()
        for a in accounts:
            if not a.enabled or a.status != "active" or a.daily_limit == 0:
                continue
            if str(a.platform) not in allowed:
                continue
            out.append({"id": a.id, "name": a.name, "platform": a.platform,
                        "left": max(0, int(a.daily_limit) - int(a.used_today)),
                        "virtual": False})
            by_platform.setdefault(a.platform, []).append(a)
        for platform, group in by_platform.items():
            code = PLATFORM_CODE.get(platform)
            if code and len(group) >= 2:
                out.append({"id": f"{code}-all",
                            "name": f"全部{PLATFORM_CN.get(platform, platform)}号({len(group)}个)",
                            "platform": platform, "left": "不限", "virtual": True})
        total = sum(len(g) for g in by_platform.values())
        if total:
            out.append({"id": "tenant-all", "name": f"全部自有账号({total}个)",
                        "platform": "multi", "left": "不限", "virtual": True})
        return out

    @staticmethod
    def resolve_real_account(channel_id, accounts):
        """发布解析：只有真实、启用、可发布的账号才返回。

        虚拟聚合 ID（`*-all`）不等于任何 Account 主键 → 天然返回 None，
        这条不变量由 test_a21 守住（否则会"发布到虚拟账号"）。
        """
        for a in accounts:
            if (a.id == channel_id and a.enabled
                    and a.status == "active" and a.daily_limit > 0):
                return a
        return None

    def list_channels(self) -> dict:
        accounts = TenantScopedRepository(Account, self.session).all()
        return {"channels": self.build_channels(accounts)}

    def list_articles(self, limit=_MAX_LIMIT) -> dict:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = _MAX_LIMIT
        limit = max(1, min(limit, _MAX_LIMIT))

        repo = TenantScopedRepository(Article, self.session)
        total = repo.query().count()
        rows = (repo.query().order_by(Article.created_at.desc(), Article.id.desc())
                .limit(limit).all())
        published = self._publish_index()
        items = []
        for r in rows:
            ck = storage.checkpoint(r.dir)
            d = storage.article_dir(r.dir)
            files = {name: (d / name).stat().st_size for name, ok in ck.items() if ok}
            entries = published.get(r.dir, [])
            items.append({
                "dir": r.dir,
                "title": r.title,
                "status": r.status,
                "files": files,
                "published": bool(entries) or r.status == "published",
                "created": hm_string(r.created_at),
                "publish_entries": entries,
            })
        return {"articles": items, "total": total}

    def _publish_index(self) -> dict:
        """成功发布日志按文章目录聚合（Phase III 执行器写库后自动生效）。"""
        repo = TenantScopedRepository(PublishLog, self.session)
        out: dict = {}
        for row in repo.query().filter(PublishLog.success.is_(True)).all():
            out.setdefault(row.article_dir or "", []).append(
                {"platform": row.platform, "success": True,
                 "url": row.remote_url or row.url or ""}
            )
        return out

    def read_file(self, dir_name: str, filename: str) -> dict:
        # 白名单必须与写侧同源（storage.ASSET_FILES）。写侧放宽而读侧停留在 STAGES，
        # 04_final_review.json 就会"写得进去、读不出来" —— 定稿复评整条特性对页面
        # 不可见。这里只收紧不放松：ASSET_FILES = STAGES + (FINAL_REVIEW,)。
        if filename not in storage.ASSET_FILES:
            raise NotFound(f"非法的文章文件名: {filename}")
        return {"name": filename, "content": storage.read_article_file(dir_name, filename)}

    def publish_log(self, limit: int = 5000) -> dict:
        """发布日志列表（root.html / publog.html 消费）。

        读 PublishLog 事实表 —— 本期表为空（发布执行器 Phase III），
        空表也返回真实形状而不是 stub 常量。
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5000
        limit = max(1, min(limit, 5000))
        repo = TenantScopedRepository(PublishLog, self.session)
        rows = (repo.query().order_by(PublishLog.happened_at.desc())
                .limit(limit).all())

        def _ts(dt) -> int:
            if dt is None:
                return 0
            from datetime import timezone
            return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())

        logs = [{
            "time": hm_string(r.happened_at),
            "time_ts": _ts(r.happened_at),
            "dir": r.article_dir or "",
            "title": r.title or "",
            "platform": r.platform,
            "account": r.account_name or "",
            "mode": r.mode or "",
            "success": bool(r.success),
            "error": r.error or "",
            # 优先回查拿到的真实链接（remote_url），退回任务 ID / 提交时的链接
            "url": r.remote_url or r.url or "",
        } for r in rows]
        return {"logs": logs, "total": len(logs)}
