"""蚁小二 CLI 的抖音图文发布驱动（YxerImgtextDriver，imageText 类型）。

与 `yxer.YxerDriver`（article 型）、`yxer_video.YxerVideoDriver`（video 型）平行，
差异只在 payload 类型：`yxer publish imageText <平台> <payload>`。

为什么单独一个驱动：抖音图文在蚁小二里是 **imageText** 类型（payload 用
`accountForms[0].images` 资源数组 + `contentPublishForm.description`），
与文章型的 `content`（HTML）/`covers` 完全不同。文章型驱动的平台表
（`_PLATFORM_CN`）里没有 `douyin_imgtext`，误用它会在调用 CLI 之前就抛
"平台不在文章发布支持范围" —— 这正是抖音图文发布 100% 失败的根因。

流程：同步 API Key → 逐张上传配图（`yxer upload --auto-meta`）→ 取首图为封面
→ init imageText 模板 → 填充 → 发布 → 返回 task_set_id（`yxer-task:<id>`，云
发布异步，不编造平台链接）。

诚实口径：抖音图文**必须有图**，无配图如实报错（不发纯文字）；未配 Key / CLI
缺失 / 账号不在云库 → 如实报错。mock 演示模式与其它驱动同一口径。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from app.logging import get_logger
from app.services.model_config import load_overrides
from app.services.publish.drivers import PublishError
from app.services.publish.imgmeta import ALLOWED_IMAGE_FORMATS, probe
from app.services.publish.yxer_cli import run_yxer, sync_api_key
from app.services.publish.yxer_cover import upload_cover

log = get_logger(__name__)

# 抖音图文/视频在蚁小二里的平台名（与 yxer_video 一致）
_PLATFORM_CN = {"douyin_imgtext": "抖音", "douyin": "抖音"}


def yxer_conf() -> dict:
    ov = load_overrides().get("yxer")
    return dict(ov) if isinstance(ov, dict) else {}


class YxerImgtextDriver:
    """经蚁小二 CLI 向单个绑定账号发布一条抖音图文（多图 + 文案）。"""

    platform = "yxer-imgtext"

    def publish(self, *, images, title: str, desc: str, tags: list,
                account, city: str | None = None,
                location: dict | None = None) -> str:
        conf = yxer_conf()
        if conf.get("use_mock"):
            slug = re.sub(r"[^\w一-鿿]+", "-", title or "")[:40].strip("-") or "imgtext"
            return f"https://mock-yxer.example.com/douyin_imgtext/{slug}"

        key = str(conf.get("token") or "").strip()
        platform_cn = _PLATFORM_CN.get(str(account.platform or ""))
        if not platform_cn:
            raise PublishError(
                f"平台 {account.platform} 不在蚁小二图文发布支持范围（仅抖音）",
                code="unknown")
        if not key:
            raise PublishError("蚁小二 API Key 未配置：请在系统设置填写（或勾选演示模式）",
                               code="login_expired")
        paths = [Path(p) for p in (images or [])]
        if not paths:
            raise PublishError("抖音图文发布需要至少一张配图（无可发布图片）",
                               code="unknown")
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise PublishError(f"配图文件缺失：{'、'.join(missing[:3])}", code="unknown")

        # 上传前本地探针：格式/尺寸不合规直接拒，别等上传后蚁小二报 schema 错
        # （实测：SVG 占位图会白传 3 张图 + 等最长 300s 超时才失败，连撞 366 次）
        probed, bad = [], []
        for p in paths:
            info = probe(p)
            if info is None or info["format"] not in ALLOWED_IMAGE_FORMATS:
                bad.append(f"{p.name}（{info['format'] if info else '无法识别'}）")
            else:
                probed.append(info)
        if bad:
            raise PublishError(
                f"配图格式抖音不支持（仅 jpg/jpeg/png/webp）：{'、'.join(bad[:3])}；"
                "请到「抖音图文 → 参数设置」把生图渠道改成在线生图，"
                "或把素材库图片换成 PNG/JPG 后重新生成",
                code="unknown")

        sync_api_key(key)                       # 应用 Key → CLI（随时切换即生效）

        uploads = [upload_cover(p) for p in paths]     # 与封面同一上传工具
        cover = uploads[0]
        # 定位（POI）：优先用发布编排**按账号分配**好的（三层不重复，见
        # services/douyin_location.py）；未传才回退"按本包城市查一个"。
        # 查不到 / 报错一律降级为「不加定位照常发」，绝不让定位拖累发布。
        if location is None:
            location = _resolve_poi(str(account.yxer_id or account.id), city)
        # 配乐（music）：抖音图文可挂背景音乐。每次从"抖音推荐榜"随机挑一首，
        # 不固定；查不到就降级为不加音乐照常发（和定位同样的原则）。
        music = _resolve_music(str(account.yxer_id or account.id))
        template = run_yxer(["publish", "init", platform_cn, "imageText"],
                            timeout=30)["data"]["payload"]
        payload = _fill_payload(template, uploads=uploads, probed=probed, cover=cover,
                                title=title, desc=desc,
                                account_id=str(account.yxer_id or account.id),
                                location=location, music=music)
        with _payload_file(payload) as path:
            data = run_yxer(["publish", "imageText", platform_cn, str(path)],
                            timeout=300)
        resp = data.get("data") or {}
        # yxer 各版本任务集 ID 字段名不一：task_set_id / taskSetId / task_id / response
        task_id = str(resp.get("task_set_id") or resp.get("taskSetId")
                      or resp.get("task_id") or resp.get("response") or "")
        if not task_id:
            raise PublishError(f"蚁小二图文发布响应缺少任务集 ID：{str(data)[:160]}",
                               code="upstream")
        return f"yxer-task:{task_id}"


def _img_entry(res: dict, local: dict) -> dict:
    """上传资源对象 + 本地探针 → schema 要的 images 项。

    宽/高/格式**以本地探针为准，CLI 返回值只作补充**：CLI 对部分格式不回这些
    字段（实测 SVG 得到 width=0/height=0/format=svg），而 schema 要求
    width/height >= 1 且 format ∈ [jpg jpeg png webp] —— 直接透传就会
    Schema validation failed。
    """
    fmt = str(res.get("format") or "").strip().lower()
    if fmt not in ALLOWED_IMAGE_FORMATS:
        fmt = str(local.get("format") or "")
    return {"key": str(res.get("key") or ""),
            "size": int(res.get("size") or 0) or int(local.get("size") or 0),
            "width": int(res.get("width") or 0) or int(local.get("width") or 0),
            "height": int(res.get("height") or 0) or int(local.get("height") or 0),
            "format": fmt}


def _fill_payload(template: dict, *, uploads: list, probed: list, cover: dict,
                  title: str, desc: str, account_id: str, location=None,
                  music=None) -> dict:
    """以平台模板为底填账号与资源字段（图文的口径与视频/文章都不同）。

    `location` 为 POI 对象（或 None）→ contentPublishForm.location（定位）；
    `music` 为背景音乐对象（或 None）→ contentPublishForm.music（配乐）。
    """
    payload = dict(template or {})
    payload["platforms"] = ["抖音"]
    args = dict(payload.get("publishArgs") or {})
    forms = [dict(f) for f in (args.get("accountForms") or [{}])] or [{}]
    form = forms[0]
    form["platformAccountId"] = account_id
    form["cover"] = cover
    form["coverKey"] = str(cover.get("key") or "")
    form["images"] = [_img_entry(r, local) for r, local in zip(uploads, probed)]
    cpf = dict(form.get("contentPublishForm") or {})
    cpf["title"] = (title or "")[:20]      # 抖音图文 title 实测只收 20 字符
    # ★ 2026-10-21：这里原是 [:64]，而 imageText 模板的 schema 限 title ≤ 20，
    # 超了整批被蚁小二拒（Schema validation failed: /title must NOT have more
    # than 20 characters）—— 表现为"该包一个号都发不出去"，不是单号问题。
    # 文章型（yxer.py）的 64 是另一套 schema，勿一起改。
    cpf["description"] = (desc or "")[:1000]      # 图文描述（非文章型的 desc）
    if location is not None:
        cpf["location"] = location                # 抖音图文定位（POI）
    if music is not None:
        cpf["music"] = music                      # 抖音图文配乐（推荐榜随机）
    form["contentPublishForm"] = cpf
    args["accountForms"] = forms
    payload["publishArgs"] = args
    return payload


# 定位（POI）与配乐（music）的查询/缓存已拆到 yxer_assets.py（本文件 300 行门禁）。
from app.services.publish.yxer_assets import (  # noqa: E402,F401
    _MUSIC_CACHE, _POI_CACHE, _POI_CACHE_FILE, _extract_location_list,
    _resolve_music, _resolve_poi)





@contextmanager
def _payload_file(payload: dict):
    """payload 落临时文件（CLI 只收文件路径），用完即删。"""
    fd, name = tempfile.mkstemp(prefix="yxer-itext-", suffix=".json")
    os.close(fd)
    path = Path(name)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
