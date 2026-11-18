"""AI 短视频产线：生成/发布队列任务 + 进程内状态桥（video.html 轮询面）。

- 生成：每条 = LLM 出分镜脚本（title/desc/tags/shots，解析失败回落模板）
  → video_provider 渲染成片+封面（未配视频供应商 = 内置演示片）→ 落
  videos/<id>/。单条 6-10 分钟量级（真实供应商），演示片秒级。
- 发布：单条 × 账号池（沿用抖音图文账号池，按最近发布最久轮换）；
  云账号走蚁小二 YxerDriver 同款出口（video 类型，封面+成片均先 upload）。
  每笔落 ledger.json（页面按账本判"已发布/可重发"）。
- 状态桥与 douyin_jobs / writer.managed 同一约定：模块 dict + 非重入 Lock，
  先取引用再进锁；单进程有效，重启清零（成片与账本在磁盘，不丢）。
"""
from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

from flask import current_app

from app.errors import UpstreamError
from app.logging import get_logger
from app.platform.jobs import task
from app.platform.state_bridge import bridge
from app.services import video_provider, video_store
from app.tenancy import current_tenant

log = get_logger(__name__)

_LOCK = threading.Lock()
_LOG_CAP = 200
_EMPTY = {"gen_running": False, "gen_stop": False, "gen_cur": 0, "gen_total": 0,
          "gen_logs": [], "pub_active": False, "pub_paused": False,
          "pub_done": 0, "pub_total": 0, "pub_logs": []}
# tid → 状态 dict（有界容器：超上限驱逐最久未访问的租户，避免只增不减）
_VD = bridge(lambda: {**_EMPTY, "gen_logs": [], "pub_logs": []})


def _st(tid: str) -> dict:
    return _VD.get(tid)


def gen_begin(tid: str, count: int) -> None:
    st = _st(tid)
    with _LOCK:
        st.update(gen_running=True, gen_stop=False, gen_cur=0,
                  gen_total=count)
        st["gen_logs"] = []


def gen_request_stop(tid: str) -> None:
    _st(tid)["gen_stop"] = True


def pub_begin(tid: str, total: int) -> None:
    st = _st(tid)
    with _LOCK:
        st.update(pub_active=True, pub_paused=False, pub_done=0, pub_total=total)
        st["pub_logs"] = []


def snapshot(tid: str) -> dict:
    st = _st(tid)
    with _LOCK:
        return {"gen_running": st["gen_running"],
                "logs": list(st["gen_logs"][-60:]) + list(st["pub_logs"][-20:])}


def reset_states() -> None:
    with _LOCK:
        _VD.clear()


def _log(st: dict, line: str) -> None:
    with _LOCK:
        st["gen_logs"].append(line)
        del st["gen_logs"][:-_LOG_CAP]


# ── 分镜脚本（LLM） ─────────────────────────────────────────────────────────

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _script(brand: str, city: str, business: str, phone: str) -> dict:
    """LLM 出分镜脚本；不可用/格式不合 → 模板兜底（产线不断流）。"""
    prompt = (
        "你是短视频编导。为下面这条本地服务业务写一条 8-12 秒竖版短视频的分镜脚本。\n"
        f"品牌：{brand}；城市：{city}；业务：{business}；电话：{phone}。\n"
        '只输出 JSON：{"title":"视频标题(20字内)","desc":"发布文案(80字内)",'
        '"tags":["话题1","话题2"],"shots":["分镜1文案","分镜2文案","分镜3文案"]}\n'
        "要求：口语化、有行动指令；shots 每条 12 字内，共 3-4 条。")
    fallback = {
        "title": f"{city}{business}实录",
        "desc": f"{business}。先检查后报价，就近上门。详询 {phone}",
        "tags": ["同城服务", "上门服务"],
        "shots": ["痛点场景引入", "服务过程实录", "成果展示+行动号召"],
    }
    try:
        from app.services.llm import build_llm_client
        from app.services.writer.output_clean import strip_fences

        # 正确用法是 client.complete(messages)：LLMClient 不可直接调用
        # （此前同一处写成 build_llm_client(...)([...])，永远抛 not callable，
        #   视频分镜脚本一直走模板兜底，与抖音图文是同一个 bug）
        raw = strip_fences(str(build_llm_client(
            current_app.config["GEO_SETTINGS"]).complete(
                [{"role": "user", "content": prompt}]) or ""))
        try:
            data = json.loads(raw)
        except ValueError:
            m = _JSON_RE.search(raw)
            data = json.loads(m.group(0)) if m else {}
        title = str(data.get("title") or "").strip()[:40]
        desc = str(data.get("desc") or "").strip()[:200]
        shots = [str(s).strip()[:24] for s in (data.get("shots") or []) if str(s).strip()][:6]
        tags = [str(t).lstrip("#").strip() for t in (data.get("tags") or []) if str(t).strip()][:8]
        if title and shots:
            return {"title": title, "desc": desc or fallback["desc"],
                    "tags": tags or fallback["tags"], "shots": shots}
    except Exception as exc:  # noqa: BLE001 - LLM 不可用 → 模板兜底
        # 兜底不能静默：否则"正常出稿"与"上游 LLM 挂了很久"在日志里毫无区别
        log.warning("video_copy_llm_fallback", extra={
            "event": "video_copy_llm_fallback", "err": str(exc)[:120]})
    return fallback


def _context() -> tuple[str, str, str, str]:
    """(brand, city, business, phone) —— 品牌/标题配置的当前租户值。"""
    from app.models import Brand, TitleConfig
    from app.services.writer.derived import extract_phone
    from app.tenancy import TenantScopedRepository

    brand_row = TenantScopedRepository(Brand).first()
    content = (brand_row.content if brand_row else "") or ""
    phone = extract_phone(content)
    cfg = TenantScopedRepository(TitleConfig).first()
    cities = str(getattr(cfg, "cities", "") or "") if cfg else ""
    city = cities.split(",")[0].strip() or "本地"
    business = str(getattr(cfg, "business", "") or "") if cfg else "本地服务"
    brand = _brand_name(content) or business
    return brand, city, business, phone


def _brand_name(content: str) -> str:
    from app.services.writer.derived import derive_brand_names
    names = derive_brand_names(content)
    return str(names["main"]) if names.get("ok") else ""


# ── 生成任务 ────────────────────────────────────────────────────────────────

@task("video.generate")
def video_generate_job(payload: dict) -> dict:
    tid = current_tenant()
    st = _st(tid)
    brand, city, business, phone = _context()
    made = 0
    try:
        sc = _script(brand, city, business, phone)
        vid = video_store.next_video_id()
        with _LOCK:
            st["gen_cur"] = vid
        _log(st, f"⏳ #{vid} 分镜脚本就绪（{len(sc['shots'])} 个分镜），开始渲染成片…")
        source = "mock"
        try:
            # 配了视频供应商就走真实生成；没配/失败如实回落演示片并在日志里说明
            # （以前无条件 render_mock，配了供应商也永远出演示片）。
            prompt = f"{sc['title']}。{sc['desc']} 分镜：" + "；".join(sc["shots"])
            mp4 = video_provider.generate_real(prompt=prompt, duration=10)
            source = "real"
        except UpstreamError as exc:
            _log(st, f"⚠ 视频供应商不可用，回落演示片：{str(exc)[:80]}")
            mp4 = video_provider.render_mock(title=sc["title"], shots=sc["shots"])
        cover = video_provider.render_cover(title=sc["title"], shots=sc["shots"])
        video_store.save_video(vid, mp4)
        video_store.save_cover(vid, cover)
        meta = {"id": vid, "title": sc["title"], "desc": sc["desc"],
                "tags": sc["tags"], "shots": sc["shots"], "city": city,
                "business": business, "duration": 10, "source": source,
                "time": video_store.now_stamp(), "published": False,
                "published_at": ""}
        video_store.write_meta(meta)
        made += 1
        _log(st, f"✅ #{vid}《{sc['title'][:20]}》成片就绪（{len(mp4) // 1024}KB），可发布")
    except Exception as exc:  # noqa: BLE001 - 单条失败不中断队列
        _log(st, f"❌ 生成失败：{str(exc)[:120]}")
    finally:
        with _LOCK:
            st["gen_running"] = False
    return {"ok": True, "made": made}


# ── 发布任务 ────────────────────────────────────────────────────────────────

@task("video.publish")
def video_publish_job(payload: dict) -> dict:
    tid = current_tenant()
    st = _st(tid)
    vid = str(payload.get("id") or "")
    try:
        url = _publish_one(vid)
        meta = video_store.read_meta(vid)
        meta["published"] = True
        meta["published_at"] = video_store.now_stamp()
        video_store.write_meta(meta)
        video_store.ledger_append(vid, {"ok": True, "account": payload.get("account", ""),
                                        "time": video_store.now_stamp(), "url": url})
        with _LOCK:
            st["pub_done"] += 1
        _log(st, f"✅ #{vid} 发布成功 {url[:48]}")
        return {"ok": True, "url": url}
    except Exception as exc:  # noqa: BLE001
        video_store.ledger_append(vid, {"ok": False, "account": payload.get("account", ""),
                                        "time": video_store.now_stamp()})
        _log(st, f"❌ #{vid} 发布失败：{str(exc)[:100]}")
        return {"ok": False, "error": str(exc)[:200]}


def _publish_one(video_id: str) -> str:
    from app.services.publish.yxer import YxerVideoDriver

    meta = video_store.read_meta(video_id)
    acct = _pick_account(video_id)
    return YxerVideoDriver().publish(
        video_path=video_store.video_path(video_id),
        cover_path=video_store.cover_path(video_id),
        title=meta.get("title") or f"视频#{video_id}",
        desc=meta.get("desc") or "", tags=meta.get("tags") or [],
        account=acct)


def _pick_account(video_id: str):
    """账号池：沿用抖音图文池（douyin_imgtext 云账号），最近发布最久优先。"""
    from app.blueprints.generation.douyin_service import DouyinService

    rows = DouyinService().accounts() or []
    pool = [a for a in rows if str(a.get("id", "")).startswith("yx-")
            and int(a.get("status") or 0) == 1]
    if not pool:
        raise ValueError("没有可用的抖音云账号：请在账号授权页勾选蚁小二抖音账号")
    # 账本按视频记账，账号的"最近发布时间"要反查（轮换 = 最久没发的优先）
    last = {a["id"]: video_store.last_account_publish_at(a["id"]) for a in pool}
    pool.sort(key=lambda a: (last.get(a["id"], ""), a["id"]))
    chosen = pool[0]
    return SimpleNamespace(id=chosen["id"], platform="douyin_imgtext",
                           yxer_id=chosen["id"][3:], name=chosen.get("name", ""))
