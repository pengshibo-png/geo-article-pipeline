"""generation 蓝图 · 抖音图文产线（douyin.html 契约编排层）。

端点面（参考系统线上行为实测）：
- GET/POST /api/douyin/config         产线配置（提示词/张数/城市/违禁词/生图渠道/计划）
- GET /api/douyin/image-providers     {providers, current, current_model}
- GET /api/douyin/posts               内容包列表（图片已转 /media/douyin URL）
- GET /api/douyin/ledger              {pack_id: [{ok,account,time}]}
- POST /api/douyin/pack/save|pack/delete   编辑（保留文件名）与删除
- GET  /api/douyin/accounts           可发抖音号（本地绑定 + 蚁小二云）
- GET  /api/douyin/status             {gen,gen_logs,posts,pub,pub_logs}（2 秒轮询）
- POST /api/douyin/generate|generate/stop|publish|publish/preview|publish/pause|publish/resume
- GET/POST /api/douyin/autopub        循环发布配置

队列/任务/状态桥在同目录 douyin_jobs.py；本文件是读多写少的编排。配置形状在
services/douyin_config.py，发布编排在 services/douyin_publish.py。
"""
from __future__ import annotations

from app.errors import NotFound
from app.logging import get_logger
from app.models import Brand, TitleConfig
from app.services import (douyin_config, douyin_store, douyin_timed, douyin_vars,
                          imagegen)
from app.services.writer.derived import derive_brand_names
from app.tenancy import TenantScopedRepository, current_tenant

from app.blueprints.generation import douyin_jobs

log = get_logger(__name__)


def _brand_name() -> str:
    row = TenantScopedRepository(Brand).first()
    try:
        names = derive_brand_names((row.content if row else "") or "")
        if names["ok"]:
            return str(names["main"])
    except Exception as exc:  # noqa: BLE001
        log.warning("douyin_brand_name_fallback", extra={
            "event": "douyin_brand_name_fallback", "err": str(exc)[:120]})
    from app.services import directory
    t = directory.get_tenant(current_tenant())
    return (t or {}).get("name", "")


class DouyinService:
    # ── 配置 ────────────────────────────────────────────────────────────────

    def get_config(self) -> dict:
        stored = douyin_store.load_config({})
        stored_f = {k: v for k, v in stored.items() if k in douyin_config.DEFAULTS}
        cfg = {**douyin_config.DEFAULTS, **stored_f}
        # 未在产线配置里显式指定生图渠道时，跟随系统设置里当前生效的生图渠道
        # （内置生图 / OpenAI 兼容 / 内置演示），让全局配置即时生效。
        if "image_channel" not in stored_f:
            cfg["image_channel"] = imagegen.current_provider()
        tc = TenantScopedRepository(TitleConfig).first()
        fallback_cities = [s.strip() for s in (tc.cities or "").replace("，", ",").split(",") if s.strip()] if tc else []
        brand_row = TenantScopedRepository(Brand).first()
        cfg["_own"] = [k for k in douyin_config.DEFAULTS if k in stored]
        cities = cfg["cities"] or fallback_cities
        cfg["_resolved"] = {
            "brand": _brand_name(),
            "phone": cfg["phone"] or self._brand_phone(),
            "cities": cities,
            # `{address}` 的预览值：此处按**首个城市**给出，逐包生成时按本篇城市重算
            # （douyin_vars.copy_vars）。页面预览与真实产出从此同一口径。
            "address": str(cfg.get("address") or "").strip()
                       or douyin_vars.default_address(cities[0] if cities else "", cities),
        }
        cfg["_brand_data_len"] = len((brand_row.content if brand_row else "") or "")
        return cfg

    def _brand_phone(self) -> str:
        from app.services.writer.derived import extract_phone
        row = TenantScopedRepository(Brand).first()
        return extract_phone((row.content if row else "") or "")

    def save_config(self, body: dict) -> dict:
        stored = douyin_store.load_config({})
        for key in douyin_config.DEFAULTS:
            if key in body:
                stored[key] = body[key]
        douyin_config.validate_counts(stored)
        douyin_store.save_config(stored)
        return {"ok": True}

    def image_providers(self) -> dict:
        provs = imagegen.providers()
        cur = imagegen.current_provider()
        cur_model = ""
        for p in provs:
            if p["id"] == cur and p["models"]:
                cur_model = p["models"][0]
        return {"providers": provs, "current": cur, "current_model": cur_model}

    # ── 内容包 / 账本 ───────────────────────────────────────────────────────

    def posts(self) -> list:
        out = []
        for p in douyin_store.list_packs():
            q = dict(p)
            q["images"] = [_img_url(p["id"], u) for u in p.get("images") or []]
            out.append(q)
        # 定时发布字段随包返回（页面卡片上的 ⏰ 与「定时」筛选都读它）
        return douyin_timed.decorate(out)

    def ledger(self) -> dict:
        return douyin_store.load_ledger()

    def pack_save(self, body: dict) -> dict:
        pid = body.get("pack_id")
        pack = douyin_store.read_pack(pid)
        # 定时时间**先校验后落盘**：写错一次不应该把同一次提交的标题/文案改动丢掉。
        at_raw = str(body.get("publish_at") or "").strip()
        if "publish_at" in body and at_raw and not douyin_timed.norm_at(at_raw):
            return {"ok": False,
                    "error": f"定时时间无法识别：{at_raw[:24]}"
                             "（应写成 2026-10-20 12:25；留空 = 取消定时）"}
        # 上限：标题 80（与页面 maxlength 一致）/ 正文 2000。**正文原来被截到 255
        # 字** —— 而产线自己的规格是"正文 600–900 字"，于是任何一次"编辑内容包→保存"
        # 都会把正文拦腰砍断、连固定尾行一起丢掉（2026-10-19 实测：796 字的稿子存成
        # 255 字，页面却显示保存成功）。上限只作越界防护，不参与正常编辑。
        for key, cap in (("title", 80), ("description", 2000)):
            if key in body:
                pack[key] = str(body[key] or "")[:cap]
        if "tags" in body:
            tags = body["tags"]
            if isinstance(tags, list):   # 页面发的是逗号串；收列表是防调用方踩坑
                tags = ",".join(str(t) for t in tags)
            pack["tags"] = [t.strip() for t in str(tags or "").split(",") if t.strip()][:10]
        if isinstance(body.get("images"), list) and body["images"]:
            pack["images"] = [str(f).split("/")[-1] for f in body["images"] if str(f).strip()]
        douyin_store.write_pack(pack)
        out: dict = {"ok": True}
        if "publish_at" in body:
            # 到点由 scheduler.tick → douyin_timed.fire 派单（一次性，发到所有可用号）
            out["publish_at"] = douyin_timed.set_at(pid, at_raw)
        return out

    def pack_delete(self, body: dict) -> dict:
        """删除内容包。失败如实回报，不让异常冒泡成 500 internal_error。"""
        try:
            douyin_store.delete_pack(body.get("pack_id"))
        except (NotFound, OSError) as exc:
            return {"ok": False, "error": f"删除失败：{str(exc)[:120]}"}
        return {"ok": True}

    # ── 生成 / 发布入队 ─────────────────────────────────────────────────────

    def start_generate(self, body: dict) -> tuple[dict, int]:
        from app.platform import jobs
        count = max(1, min(int(body.get("count") or 1), 30))
        cfg = self.get_config()
        # 判「有没有城市」要看解析后的池子（空=自动继承业务城市），不能看原始配置——
        # 否则把"推广城市"留空（本意就是用业务城市）会被这里误判为"未填写投放城市"。
        if not (cfg.get("_resolved") or {}).get("cities"):
            return ({"ok": False, "error": "请先在产线配置里填写投放城市（图文按城市轮换出稿）"}, 400)
        # 不再要求"出图提示词池"：画风由 style_prompt 决定，留空即用内置风格；
        # 图纲由 LLM 按文案规划，规划失败时用文案首句自动兜底，无需人工维护池子。
        # 库存上限（max_stock）：无人值守的定时发布 + 反复手点会无限堆积（实测
        # 46 个包已占 469MB）。到顶如实拒绝；生成侧还有一道同样的闸（自动补货
        # 不经这个端点）。
        stock = len(douyin_store.list_packs())
        if stock >= int(cfg.get("max_stock") or 80):
            return ({"ok": False,
                     "error": f"内容包库存已满（{stock} 个，上限 {cfg.get('max_stock')}）："
                              "请先发布或删除旧内容包再生成"}, 400)
        douyin_jobs.gen_begin(current_tenant(), count)
        jobs.enqueue("douyin.generate.batch", {"count": count},
                     tenant_id=current_tenant(), queue="gen")
        return ({"ok": True, "queued": count}, 200)

    def stop_generate(self) -> dict:
        douyin_jobs.gen_request_stop(current_tenant())
        return {"ok": True}

    def publish(self, body: dict) -> tuple[dict, int]:
        from app.platform import jobs
        from app.services import douyin_accounts, douyin_publish
        pack_ids = [str(x) for x in (body.get("pack_ids") or [])]
        account_ids = [str(x) for x in (body.get("account_ids") or [])]
        if not pack_ids or not account_ids:
            return ({"ok": False, "error": "pack_ids 与 account_ids 必填"}, 400)
        accs = self.accounts()
        known = {a["id"] for a in accs}
        if not all(a in known for a in account_ids):
            return ({"ok": False, "error": "存在失效账号，请重新选择"}, 400)
        mode = douyin_publish.resolve_mode(body.get("mode"), self.get_config())
        # 报给页面的条数必须**等于实际会发的条数**（与 job 共用 douyin_publish.plan）。
        # 叉乘时代页面显示 6 条、实际发 3 条；2026-10-19 又把"勾了 12 个号只发 1 条"
        # 反过来投诉了一次 —— 所以条数与跳过原因都如实回报。
        pairs, notes = douyin_publish.plan(pack_ids, account_ids, accs, mode=mode)
        douyin_jobs.pub_begin(current_tenant(), len(pairs))
        douyin_accounts.clear_cache()     # 发完一遍后预览要重新读云账号
        jobs.enqueue("douyin.publish.batch",
                     {"pack_ids": pack_ids, "account_ids": account_ids, "mode": mode,
                      "locations": body.get("locations") or []},
                     tenant_id=current_tenant(), queue="publish")
        return ({"ok": True, "queued": len(pairs), "notes": notes[:6]}, 200)

    def preview_publish(self, body: dict) -> dict:
        """发布**前**算一遍真实条数（与真正执行共用同一份计划）。

        页面弹窗要在用户点"发布"之前就把"将发出 N 条"写清楚 —— 原来是前端
        自己估（`n × m`），估的规则与真跑的规则可以不一致（实测"1 个包 × 12 个号，
        每号约 1 包"，真发出去 1 条）。
        """
        from app.services import douyin_accounts, douyin_publish
        mode = douyin_publish.resolve_mode(body.get("mode"), self.get_config())
        # 预览用短缓存读云账号（勾一个号算一次，实时读要 0.4–1.0s/次）；真正派单
        # 走 publish() 的实时池，两处口径同一份 plan()。
        pairs, notes = douyin_publish.plan(
            [str(x) for x in (body.get("pack_ids") or [])],
            [str(x) for x in (body.get("account_ids") or [])],
            douyin_accounts.list_accounts(cached=True), mode=mode)
        return {"ok": True, "queued": len(pairs), "mode": mode, "notes": notes[:6]}

    def publish_pause(self) -> dict:
        return {"ok": douyin_jobs.pub_set_paused(current_tenant(), True)}

    def publish_resume(self) -> dict:
        return {"ok": douyin_jobs.pub_set_paused(current_tenant(), False)}

    def status(self) -> dict:
        snap = douyin_jobs.snapshot(current_tenant())
        snap["posts"] = _recent_packs()
        return snap

    # ── 账号（本地绑定 + 蚁小二云）──────────────────────────────────────────
    # 池子在 services/douyin_accounts.py：发布计划只对这个函数的返回值派单 ——
    # "哪些号能发"两边只能有一个答案（页面勾得上的号必须就是真会发的号）。

    def accounts(self) -> list:
        from app.services import douyin_accounts

        return douyin_accounts.list_accounts()

    # ── 循环发布（配置存取；触发在 services/douyin_schedule.py + scheduler）───

    def autopub_get(self) -> dict:
        from app.services import douyin_schedule
        from app.timeutil import now_local

        cfg = douyin_store.load_autopub()
        led = douyin_store.load_ledger()
        last_ts = None
        for records in led.values():
            for r in records:
                last_ts = max(last_ts or 0, douyin_config.ts_of(r))
        # next_in_sec：页面里的倒计时无条件读它（`j.next_in_sec`），而配置里从来没
        # 这个字段 → 倒计时恒为 "NaN:NaN"。真实值由 douyin_schedule 按模式算。
        return {"config": cfg, "last_pub_ts": last_ts,
                "next_in_sec": douyin_schedule.next_in_sec(cfg, now_local()),
                "stock": [{"name": a["name"], "online": a["status"] == 1,
                           "remaining": a.get("remaining")}
                          for a in self.accounts() if a.get("cloud")]}

    def autopub_save(self, body: dict) -> dict:
        from app.services import douyin_schedule   # 归一与校验：与定时触发同一模块

        # 整段读-改-写持锁：调度线程可能正同时改同一份 JSON（记 last_run /
        # last_shot），无锁会互相覆盖 → 要么重复发稿，要么把"当天已发"的标记丢掉。
        with douyin_store.autopub_lock():
            prev = douyin_store.load_autopub()
            clean = douyin_schedule.build_config(body, prev)
            douyin_store.save_autopub(clean)
        # 「保存并立即发一篇」：前端显式传 reset_timer。以前后端白名单不读它，
        # 按钮等同"保存"（点了没反应）；现在按需立刻派发一篇。**放在锁外**：
        # fire 内部还会取同一把锁，把入队动作放在锁内会拖住调度线程。
        if clean["enabled"] and body.get("reset_timer"):
            from app.services import scheduler
            from app.timeutil import now_local
            scheduler._fire_douyin_autopub(current_tenant(), now_local(), force=True)
        return {"ok": True}


def _recent_packs() -> list:
    """本批生成真实产出的内容包（状态桥 gen_made）。

    以前返回 `list_packs()[-30:]`，页面上的「本次已出 N 包」与"本次"无关 ——
    生成 3 条也可能显示 30。现在按批记录。
    """
    from app.blueprints.generation import douyin_jobs
    from app.tenancy import current_tenant

    st = douyin_jobs.snapshot(current_tenant())
    return list(st.get("gen_made") or [])


def _img_url(pack_id, ref: str) -> str:
    if str(ref).startswith(("http", "data:")):
        return str(ref)
    return f"/media/douyin/{pack_id}/{str(ref).split('/')[-1]}"
