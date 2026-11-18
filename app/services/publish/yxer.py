"""蚁小二（yxer）内容发布集成（CLI 模式，2026-10-14 起）。

安装（skillhub 安装文档）：`npm install -g @yixiaoermail/cli` → `yxer --version` →
`yxer config set-api-key <key>` → `yxer doctor`。授权登录在蚁小二客户端管理，
本系统只做**发布内容**：

① `YxerDriver.publish` —— 账号绑定了蚁小二云账号（Account.yxer_id）时的发布出口：
   同步 API Key → 取平台发布模板（`yxer publish init`，平台差异由模板兜底）→
   解析并上传封面 → 组装 payload → `yxer publish` 云发布 → 返回任务集 ID。
   云发布是异步的，返回 `task_set_id`（execution-standard §5），以
   `yxer-task:<id>` 记入发布日志 —— 不编造平台链接。
② `list_accounts` —— 云账号列表（`yxer accounts list`），账号库页勾选同步后
   Account.yxer_id 即发布用的 platformAccountId。

**API Key 随时切换**：系统设置里的「蚁小二 API Key」就是 CLI 的 apiKey，
每次发布前同步进 CLI 配置（不同才写，见 yxer_cli.sync_api_key）——
改 Key = 换蚁小二账号库，即时生效、无需重启。

平台范围：仅文章型平台（见 `_PLATFORM_CN`）。抖音归图文产线（imageText，
另一套 payload），美篇/列表网/58 不在蚁小二目录 —— 如实报错，不硬发。

mock 演示模式（use_mock）：离线确定性假发布，与 FakeDriver 同一诚实口径。

配置存 `data/model_config.json` 的 `yxer` 段：`{"token": "<API Key>",
"use_mock": false}`（base_url/各 path 为旧 HTTP 集成的遗留字段，CLI 模式忽略）。
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
from app.services.publish.yxer_cover import cover_entry, resolve_cover, upload_cover

log = get_logger(__name__)

# 平台短码 → 蚁小二平台中文名（platform.md 枚举中支持 article 发布的部分）
_PLATFORM_CN = {
    "souhu": "搜狐号", "baijiahao": "百家号", "toutiao": "头条号",
    "wangyi": "网易号", "zhihu": "知乎", "jianshu": "简书", "dayu": "大鱼号",
    "yidian": "一点号", "qie": "企鹅号", "xinlang": "新浪微博", "douban": "豆瓣",
    "csdn": "CSDN", "bilibili": "哔哩哔哩", "acfun": "AcFun", "aiqiyi": "爱奇艺",
    "chejia": "车家号", "yiche": "易车号", "xueqiu": "雪球号",
    "kuaichuan": "快传号", "weixin": "微信公众号",
}
# 反向映射（账号列表用）；抖音归图文产线账号池（douyin_service 按 douyin 前缀过滤）
_CN_TO_CODE = {v: k for k, v in _PLATFORM_CN.items()}
_CN_TO_CODE["抖音"] = "douyin_imgtext"

# 对外暴露的文章型平台白名单：文章发布渠道列表按它过滤，避免把抖音图文这类
# 图文/视频平台混进来（选了必然报"平台不在文章发布支持范围"）。
ARTICLE_PLATFORMS = frozenset(_PLATFORM_CN)

_TAG_RE = re.compile(r"<[^>]+>")


def yxer_conf() -> dict:
    ov = load_overrides().get("yxer")
    return dict(ov) if isinstance(ov, dict) else {}


def is_configured(conf: dict | None = None) -> bool:
    conf = conf if conf is not None else yxer_conf()
    return bool(str(conf.get("token") or "").strip()) or bool(conf.get("use_mock"))


# ── 云账号列表（账号库页 / 抖音产线账号池共用） ─────────────────────────────

def list_accounts() -> dict:
    """→ {configured, accounts:[{id,name,platform,fans,online}], error?}"""
    conf = yxer_conf()
    if conf.get("use_mock"):
        return {"configured": True, "mock": True, "accounts": _mock_accounts()}
    key = str(conf.get("token") or "").strip()
    if not key:
        return {"configured": False, "accounts": []}
    try:
        sync_api_key(key)
        data = run_yxer(["accounts", "list", "--all"], timeout=30)
    except PublishError as exc:
        return {"configured": True, "accounts": [],
                "error": f"蚁小二账号读取失败：{exc.message[:140]}"}
    accounts = []
    for a in data.get("data") or []:
        if not isinstance(a, dict):
            continue
        accounts.append({
            "id": str(a.get("id") or ""),
            "name": str(a.get("platformAccountName") or a.get("name") or "未命名账号"),
            "platform": _CN_TO_CODE.get(str(a.get("platformName") or ""), "other"),
            "fans": int(a.get("fans") or a.get("follower") or 0),
            "online": int(a.get("status") or 0) == 1,
        })
    return {"configured": True, "accounts": [a for a in accounts if a["id"]]}


def _mock_accounts() -> list:
    return [
        {"id": "yx-souhu-1", "name": "搜狐号A（蚁小二）", "platform": "souhu", "fans": 0, "online": True},
        {"id": "yx-souhu-2", "name": "搜狐号B（蚁小二）", "platform": "souhu", "fans": 0, "online": True},
        {"id": "yx-baijiahao-1", "name": "百家号A（蚁小二）", "platform": "baijiahao", "fans": 0, "online": True},
        {"id": "yx-meipian-1", "name": "美篇号A（蚁小二）", "platform": "meipian", "fans": 0, "online": True},
        {"id": "yx-xiaohongshu-1", "name": "小红书A（蚁小二）", "platform": "xiaohongshu", "fans": 0, "online": False},
        {"id": "yx-douyin-1", "name": "抖音号A（蚁小二）", "platform": "douyin_imgtext", "fans": 1000, "online": True},
        {"id": "yx-douyin-2", "name": "抖音号B（蚁小二）", "platform": "douyin_imgtext", "fans": 0, "online": True},
    ]


# ── 发布驱动（接入 publish 状态机） ─────────────────────────────────────────

class YxerDriver:
    """经蚁小二 CLI 向单个绑定账号分发一篇内容（文章型平台）。"""

    platform = "yxer"

    def publish(self, *, html: str, title: str, account) -> str:
        conf = yxer_conf()
        if conf.get("use_mock"):
            slug = re.sub(r"[^\w一-鿿]+", "-", title or "")[:40].strip("-") or "article"
            return f"https://mock-yxer.example.com/{account.platform}/{slug}"

        key = str(conf.get("token") or "").strip()
        platform_cn = _PLATFORM_CN.get(str(account.platform or ""))
        if not platform_cn:
            raise PublishError(
                f"平台 {account.platform} 不在蚁小二文章发布支持范围"
                f"（支持：{'、'.join(sorted(set(_PLATFORM_CN.values())))}）", code="unknown")
        if not key:
            raise PublishError("蚁小二 API Key 未配置：请在系统设置填写（或勾选演示模式）",
                               code="login_expired")
        sync_api_key(key)                       # 应用 Key → CLI（随时切换即生效）

        cover = upload_cover(resolve_cover(html))
        template = run_yxer(["publish", "init", platform_cn, "article"],
                            timeout=30)["data"]["payload"]
        payload = _fill_payload(template, title=title, html=html, cover=cover,
                                platform_cn=platform_cn,
                                account_id=str(account.yxer_id or account.id))
        with _payload_file(payload) as path:
            data = run_yxer(["publish", "article", platform_cn, str(path)], timeout=180)
        resp = data.get("data") or {}
        # yxer 各版本任务集 ID 字段名不一：task_set_id / taskSetId / task_id / response
        # 都可能承载它；只认 task_set_id 会把 ok:true 的响应误判为失败（3.2.28 实测）。
        task_id = str(resp.get("task_set_id") or resp.get("taskSetId")
                      or resp.get("task_id") or resp.get("response") or "")
        if not task_id:
            raise PublishError(f"蚁小二发布响应缺少任务集 ID：{str(data)[:160]}",
                               code="upstream")
        return f"yxer-task:{task_id}"


def _desc_of(html: str) -> str:
    """摘要 = 正文去标签取前 200 字（蚁小二 desc 必填；不编造，就摘原文）。"""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html or "")).strip()[:200]


def _fill_payload(template: dict, *, title: str, html: str, cover: dict,
                  platform_cn: str, account_id: str) -> dict:
    """以平台模板为底填业务字段（平台差异字段保留模板默认值）。"""
    payload = dict(template or {})
    payload["platforms"] = [platform_cn]
    desc = _desc_of(html)
    # 同 contentPublishForm.desc：根节点 strict schema 的平台不接受凭空字段，
    # 只在模板本就带 desc 时才回填（否则 Schema validation failed）。
    if "desc" in payload:
        payload["desc"] = desc
    args = dict(payload.get("publishArgs") or {})
    entry = cover_entry(cover)
    args["cover"] = cover                     # preflight 要完整上传资源对象
    args["coverKey"] = entry["key"]
    args["content"] = html                    # CLI 会补齐内层副本并清理外层
    forms = [dict(f) for f in (args.get("accountForms") or [{}])] or [{}]
    form = forms[0]
    form["platformAccountId"] = account_id
    cpf = dict(form.get("contentPublishForm") or {})
    # ★ 标题上限 30 字（2026-11-03 用户要求）：超过 30 字在很多号上会被平台
    # schema 拒（整批发不出去），比「丢副标题」严重得多。这里用**智能压缩**
    # （按问号 / 冒号 / 分句边界）而不是硬截断 —— 硬切会切出「…透明车间与」
    # 这种半截词。生成侧 `normalize_title` 已经压到 30，这里是发布前的最后兜底。
    from app.services.writer.title_context import shortify
    cpf["title"] = shortify(title, 30)[:30]
    # desc 只回填模板本就带该字段的平台：部分平台 schema 禁止额外字段
    # （头条号 contentPublishForm additionalProperties:false，凭空加 desc 会 Schema validation failed）
    if "desc" in cpf:
        cpf["desc"] = desc
    cpf["covers"] = [entry]
    form["contentPublishForm"] = cpf
    args["accountForms"] = forms
    payload["publishArgs"] = args
    return payload


@contextmanager
def _payload_file(payload: dict):
    """payload 落临时文件（CLI 只收文件路径），用完即删。"""
    fd, name = tempfile.mkstemp(prefix="yxer-payload-", suffix=".json")
    os.close(fd)
    path = Path(name)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
