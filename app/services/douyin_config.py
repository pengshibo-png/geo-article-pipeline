"""抖音产线配置的形状（默认值 + 归一/校验）。

从 `blueprints/generation/douyin_service.py` 拆出（A3 单文件 300 行预算）——
本模块只描述"一份产线配置长什么样"，不含任何 HTTP/编排逻辑，生成侧
（`douyin_pack` / `douyin_visual`）与配置侧读到的是同一份默认值。
"""
from __future__ import annotations

from app.errors import ValidationError
from app.services import imagegen

DEFAULTS = {
    "creative_prompt": "围绕本地上门服务的真实场景：客户遇到的问题、师傅怎么处理、处理后的效果",
    "copy_prompt": "口语化、有现场感，突出就近上门与先检查后报价，避免夸大承诺",
    # ★ 海报画风（STYLE）：**空 = 用代码里的默认风格**（douyin_visual.STYLE，
    # 米白/淡绿底的金色科普信息图）。填了就**整段替换**那套画风底座 ——
    # 这样改风格不用改代码、不用重新打包（2026-11-05 用户要求）。
    "style_prompt": "",
    "image_count_min": 4, "image_count_max": 6,
    "phone": "", "cities": [], "banned_words": [],
    # 服务行内容（`{address}`）：会用在文案末行与海报底部行。空 = 按城市自动生成
    # （页面预览里那行"城市·全国N城就近上门"）。
    "address": "",
    "image_size": "1536x2720", "image_channel": imagegen.MOCK_ID, "image_model": "",
    # 内容包库存上限：无人值守的循环发布 + 反复手点会无限堆积
    # （实测 46 个包已占 469MB），到顶就拒绝生成，让用户先发布/清理。
    "max_stock": 80,
    # 分发口径（见 douyin_publish.MODES）：each=每个勾选的号各发一份；
    # rotate=轮流分配（一包只发一个号）。页面弹窗里可改，改了就记住了。
    "publish_mode": "each",
    "schedule": [],
    # 选题的联网搜索线索（热词/关注点）：选题规划器会读它，作为额外输入。
    "topic_hint": "",
}


def clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def validate_counts(cfg: dict) -> None:
    """配图数量区间与列表字段归一（越界纠正，区间倒置 400）。"""
    lo = clamp(cfg.get("image_count_min"), 1, 9, 4)
    hi = clamp(cfg.get("image_count_max"), 1, 9, 6)
    if lo > hi:
        raise ValidationError("图片张数区间不合法（最小不能大于最大）")
    cfg["image_count_min"], cfg["image_count_max"] = lo, hi
    cfg["cities"] = [c for c in (cfg.get("cities") or []) if str(c).strip()][:50]
    # 画风：空串表示"用代码默认"，有值就限长（超长会把正文挤出发图提示词）
    cfg["style_prompt"] = str(cfg.get("style_prompt") or "").strip()[:3000]


def used_today(account) -> int:
    """账号今日已发条数（跨日自动归零）。与文章发布一侧同一口径。"""
    from app.timeutil import natural_day_string

    return int(account.used_today) if str(account.quota_date) == natural_day_string() else 0


def ts_of(record: dict) -> int:
    from datetime import datetime

    try:
        return int(datetime.strptime(record.get("time") or "", "%Y-%m-%d %H:%M:%S").timestamp())
    except ValueError:
        return 0


# ── 自动发布（autopub）配置 ─────────────────────────────────────────────────
# 与上面"产线配置"同一件事的两半：一份是生成侧的形状，一份是发布计划的形状。
# 从 douyin_schedule 搬来（A3 行数预算），那边再导入回去，调用点零改动。

_MAX_SLOTS = 12                # 最多几个时段（一天 12 次已经很多，再多易触发平台限流）


def norm_time(raw) -> str:
    """把 "9:5" / "09：05" 这类输入归一成 "09:05"；非法返回空串。"""
    text = str(raw or "").strip().replace("：", ":")
    parts = text.split(":")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        return ""
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return ""
    return f"{hh:02d}:{mm:02d}"


def norm_schedule(raw) -> list[dict]:
    """时段表归一：非法时刻丢弃、按时间排序、条数钳到 1–10、最多 12 档。

    每档还可带两个可选字段：
    - `days`：只在周几发（0=周一 … 6=周日）。**空 = 每天**。
    - `account_ids`：这一档固定发到哪个号。**空 = 用卡片里勾选的那批轮流分配**。
    """
    out: list[dict] = []
    for item in (raw or [])[:_MAX_SLOTS]:
        if not isinstance(item, dict):
            continue
        t = norm_time(item.get("time"))
        if not t:
            continue
        days: list[int] = []
        for d in (item.get("days") or []):
            try:
                di = int(d)
            except (TypeError, ValueError):
                continue
            if 0 <= di <= 6 and di not in days:
                days.append(di)
        out.append({"time": t,
                    "count": clamp(item.get("count"), 1, 10, 1),
                    "enabled": bool(item.get("enabled", True)),
                    "days": sorted(days),
                    "account_ids": [str(x)[:64] for x in (item.get("account_ids") or [])][:10]})
    return sorted(out, key=lambda x: x["time"])


def build_autopub(body: dict, prev: dict) -> dict:
    """把页面提交的 autopub 配置归一成落盘格式（含校验 + 保留防重标记）。"""
    from app.services import douyin_store

    clean = {
        "enabled": bool(body.get("enabled")),
        # 发布模式：interval=每 N 分钟发一篇；schedule=每天固定时刻发 N 条
        "mode": "schedule" if body.get("mode") == "schedule" else "interval",
        "schedule": norm_schedule(body.get("schedule")),
        "interval_min": clamp(body.get("interval_min"), 5, 720, 30),
        "order": "old" if body.get("order") == "old" else "new",
        "auto_gen": bool(body.get("auto_gen", True)),
        "gen_mode": (body.get("gen_mode")
                     if body.get("gen_mode") in ("stock", "interval", "off") else "stock"),
        "gen_every_min": clamp(body.get("gen_every_min"), 5, 1440, 60),
        "gen_batch": clamp(body.get("gen_batch"), 1, 20, 5),
        # 自动发布的分发口径：each=一包发全部账号（一包多发）；rotate=一包只落一个号。
        # 默认 rotate（保持老的自动发布行为），显式传/存过 each 就记住。
        "publish_mode": ("each" if (body.get("publish_mode")
                                    or prev.get("publish_mode")) == "each" else "rotate"),
        "account_ids": [str(x)[:64] for x in (body.get("account_ids") or [])][:50],
        # 防重标记必须保留：丢了会让同一时刻当天被重复触发；last_gen 丢了会让
        # "每隔 N 分钟补货"每次都判定"到点了"，一保存就连环补货。
        "last_shot": prev.get("last_shot") or {},
        "last_run": prev.get("last_run") or "",
        "last_gen": prev.get("last_gen") or "",
    }
    if clean["enabled"] and not clean["account_ids"]:
        raise ValidationError("开启自动发布需至少勾选一个抖音号")
    if clean["enabled"] and clean["mode"] == "schedule" \
            and not any(s["enabled"] for s in clean["schedule"]):
        raise ValidationError("定时发布至少要有一个启用的时刻")
    # 首次启用：last_run 置现在，保证"保存≠立即跑"，满一个间隔才发
    if clean["enabled"] and not prev.get("enabled"):
        clean["last_run"] = douyin_store.now_stamp()
    return clean