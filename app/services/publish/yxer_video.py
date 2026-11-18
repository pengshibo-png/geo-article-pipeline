"""蚁小二 CLI 的视频发布驱动（YxerVideoDriver，video 类型）。

与 `yxer.YxerDriver`（文章型）平行的出口，差异只在 payload 类型：
`yxer publish video <平台> <payload>`，模板（`publish init <平台> video`）把
cover / coverKey / video（上传资源对象）放在 accountForms[0] 内。

流程：同步 API Key → 上传封面 PNG → 上传成片 MP4（均 `yxer upload --auto-meta`，
视频元数据由 CLI 解出 duration/width/height）→ init 模板 → 填充 → 发布 →
返回 task_set_id（`yxer-task:<id>`，云发布异步，不编造平台链接）。

平台范围：抖音（竖版短视频）。未配 Key / CLI 缺失 / 账号不在云库 → 如实报错。
mock 演示模式与文章驱动同一口径（确定性假链接，不假装发出去）。
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
from app.services.publish.yxer_cli import run_yxer, sync_api_key
from app.services.publish.yxer_cover import upload_cover

log = get_logger(__name__)

_PLATFORM_CN = {"douyin_imgtext": "抖音", "douyin": "抖音"}


def yxer_conf() -> dict:
    ov = load_overrides().get("yxer")
    return dict(ov) if isinstance(ov, dict) else {}


class YxerVideoDriver:
    """经蚁小二 CLI 向单个绑定账号发布一条竖版短视频。"""

    platform = "yxer-video"

    def publish(self, *, video_path, cover_path, title: str, desc: str,
                tags: list, account) -> str:
        conf = yxer_conf()
        if conf.get("use_mock"):
            slug = re.sub(r"[^\w一-鿿]+", "-", title or "")[:40].strip("-") or "video"
            return f"https://mock-yxer.example.com/video/{account.platform}/{slug}"

        key = str(conf.get("token") or "").strip()
        platform_cn = _PLATFORM_CN.get(str(account.platform or ""))
        if not platform_cn:
            raise PublishError(
                f"平台 {account.platform} 不在蚁小二视频发布支持范围（仅抖音）",
                code="unknown")
        if not key:
            raise PublishError("蚁小二 API Key 未配置：请在系统设置填写（或勾选演示模式）",
                               code="login_expired")
        if not Path(video_path).exists() or not Path(cover_path).exists():
            raise PublishError("成片或封面文件缺失", code="unknown")
        sync_api_key(key)                       # 应用 Key → CLI（随时切换即生效）

        video = run_yxer(["upload", str(video_path), "--auto-meta"],
                         timeout=300)["data"]
        cover = upload_cover(Path(cover_path))
        template = run_yxer(["publish", "init", platform_cn, "video"],
                            timeout=30)["data"]["payload"]
        payload = _fill_payload(template, video=video, cover=cover,
                                account_id=str(account.yxer_id or account.id))
        with _payload_file(payload) as path:
            data = run_yxer(["publish", "video", platform_cn, str(path)], timeout=300)
        resp = data.get("data") or {}
        # yxer 各版本任务集 ID 字段名不一：task_set_id / taskSetId / task_id / response
        task_id = str(resp.get("task_set_id") or resp.get("taskSetId")
                      or resp.get("task_id") or resp.get("response") or "")
        if not task_id:
            raise PublishError(f"蚁小二视频发布响应缺少任务集 ID：{str(data)[:160]}",
                               code="upstream")
        return f"yxer-task:{task_id}"


def _fill_payload(template: dict, *, video: dict, cover: dict,
                  account_id: str) -> dict:
    """以平台模板为底填账号与资源字段（desc/tags 走模板允许的字段）。"""
    payload = dict(template or {})
    args = dict(payload.get("publishArgs") or {})
    forms = [dict(f) for f in (args.get("accountForms") or [{}])] or [{}]
    form = forms[0]
    form["platformAccountId"] = account_id
    form["cover"] = cover
    form["coverKey"] = str(cover.get("key") or "")
    form["video"] = {"key": str(video.get("key") or ""),
                     "size": int(video.get("size") or 0),
                     "width": int(video.get("width") or 0),
                     "height": int(video.get("height") or 0),
                     "duration": int(video.get("duration") or 0),
                     "format": str(video.get("format") or "mp4")}
    args["accountForms"] = forms
    payload["publishArgs"] = args
    return payload


@contextmanager
def _payload_file(payload: dict):
    """payload 落临时文件（CLI 只收文件路径），用完即删。"""
    fd, name = tempfile.mkstemp(prefix="yxer-vpayload-", suffix=".json")
    os.close(fd)
    path = Path(name)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
