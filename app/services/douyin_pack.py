"""抖音图文内容包组装：生图 + LLM 文案 → 落盘 pack.json。

从 `blueprints/generation/douyin_jobs.py` 拆出（ADR §1.5 单文件 300 行预算）。
迁出后这里额外承担两件"产线不白跑"的事：

- **单张降级**：在线生图连续失败（含重试）时用内置占位图顶上，并在 `qc` 里
  标注。原实现是一张超时即整条内容包报废 —— 3 张图挂 1 张等于全白做。
- **整条回滚**：组装异常时删掉已写目录，不留"只有 images/、没有 pack.json"
  的孤儿目录（孤儿页面看不到、没人清理，还会因 next_pack_id 取目录名最大值+1
  而吃掉编号，用户会看到跳号的内容包）。
"""
from __future__ import annotations

import base64
import json
import random
import re

from flask import current_app

from app.logging import get_logger
from app.platform import net
from app.services import douyin_store, douyin_vars, imagegen
from app.services.douyin_visual import image_plan_for

log = get_logger(__name__)

_IMG_TIMEOUT = 120.0          # 下载方舟 TOS 图片（1536x2720 约 5~6MB）

_JSON_RE = re.compile(r"\{.*\}", re.S)


# 「业务城市」两步覆盖并入的 25 个地级市（与区/县/县级市相对）；抖音只从区/县/县级市随机挑，新增地级市请同步加进此集合。
_PREFECTURE_CITIES = {
    "上海", "南京", "无锡", "徐州", "常州", "苏州", "南通", "连云港", "淮安",
    "盐城", "扬州", "镇江", "泰州", "宿迁", "杭州", "宁波", "温州", "嘉兴",
    "湖州", "绍兴", "金华", "衢州", "舟山", "台州", "丽水",
}


def build_pack(cfg: dict) -> dict:
    """组装一个内容包（文案 → 据此配图 → 质检），全部成功才落 pack.json。

    """
    cities = (cfg.get("_resolved") or {}).get("cities") or cfg.get("cities") or [""]
    # 抖音图文只从区/县/县级市随机挑（不取地级市名），让标题与定位都落具体区县
    city = pick_district_city(cities)

    brand = (cfg.get("_resolved") or {}).get("brand") or ""
    phone = (cfg.get("_resolved") or {}).get("phone") or ""
    lo = int(cfg.get("image_count_min") or 4)
    hi = max(lo, int(cfg.get("image_count_max") or lo))
    n = random.randint(lo, hi)

    pid = douyin_store.next_pack_id()
    try:
        from app.services import douyin_topics

        topic = douyin_topics.next_topic(cfg)
        title, desc, tags, from_llm = copy_for(cfg, city, brand, phone, topic=topic)
        if from_llm:
            douyin_topics.mark_used(topic)
        # 保障：文案没走 AI 就不进配图（避免"通用标题+图上串字"的低质包被发出去）
        if not from_llm:
            raise RuntimeError("文案生成失败：连续 3 次均被判重或结构不合格，已跳过本条（不生成配图）")
        # 标签优先用"创意一体化指令"（creative_prompt）摘出来的那组：页面对这条指令
        # 的说明就是"标签 + 每张海报素材摘录"，文案侧自带的那组只作兜底。
        prompts, plan_tags = image_plan_for(cfg, f"{title}\n\n{desc}", city, n)
        tags = plan_tags or tags
        images, degraded, forced_mock, url_fallback = _render_images(
            pid, cfg, prompts, n, city)
        qc = []
        if not from_llm:
            # 让"文案是兜底"在作品卡片上看得见（qc 会渲染成质检警告）
            qc.append("文案为兜底模板（AI 未生效，建议手改后再发）")
        if forced_mock:
            qc.append("在线生图未生效（Key 缺失或失效），全部配图为内置占位图，"
                      "发布前务必替换")
        elif degraded:
            qc.append("第 " + "、".join(str(k) for k in degraded) +
                      " 张为内置占位图（在线生图连续失败，建议替换后再发布）")
        if url_fallback:
            qc.append("第 " + "、".join(str(k) for k in url_fallback) +
                      " 张未能下载到本地，存的是外部链接（约 24 小时失效，"
                      "发布前请手动替换，否则次日裂图）")
        text = f"{title}{desc}"
        for w in cfg.get("banned_words") or []:
            if w and str(w) in text:
                qc.append(f"疑似违禁词「{w}」")
        pack = {"id": pid, "title": title, "description": desc, "tags": tags,
                "images": images, "time": douyin_store.now_stamp(),
                "city": city, "brand": brand, "phone": phone, "qc": qc}
        return douyin_store.write_pack(pack)
    except Exception:
        # 回滚已写的图：宁可这次什么都不留，也不留一个查不到原因的孤儿目录
        douyin_store.discard_pack(pid)
        raise


def pick_district_city(cities) -> str:
    """抖音图文取城市：只从区/县/县级市随机挑，排除地级市名（见 _PREFECTURE_CITIES）。"""
    district = [c for c in cities if c and c not in _PREFECTURE_CITIES]
    return random.choice(district or [c for c in cities if c] or [""])


def _render_images(pid: int, cfg: dict, prompts: list, n: int,
                   city: str) -> tuple[list, list, bool, list]:
    """逐张生图并落盘，返回 (文件名列表, 降级序号, 是否整批被迫用占位图)。

    单张失败不终止产线：`imagegen.generate` 内部已重试过，仍失败说明这一张
    确实取不到，于是用内置占位图顶上 —— 内容包照常可用，但**在 qc 里如实标注**，
    不会把占位图伪装成真实生成图。

    `forced_mock` 专指**配置了在线渠道但 Key 缺失/失效**：`imagegen.generate`
    在这种情况下会**静默**返回内置占位图（它自己认为"未配置"，不抛错）。
    与"抖音电话填占位号"是同一类错误——占位值悄悄覆盖真实产出，必须显式标注。
    """
    size = str(cfg.get("image_size") or "1024x1024")
    channel = cfg.get("image_channel") or None
    # 产线配置里的"生图模型"：以前是死配置（页面存得下，上游始终用系统设置里的模型）
    model = str(cfg.get("image_model") or "").strip() or None
    forced_mock = (bool(channel) and channel != imagegen.MOCK_ID
                   and not imagegen.is_configured())
    if forced_mock:
        log.warning("douyin_image_key_missing", extra={
            "event": "douyin_image_key_missing", "pack": pid, "channel": channel})
    images: list = []
    degraded: list = []
    url_fallback: list = []      # 下载失败、只能存原始 URL 的图（次日会裂图）
    # 不再按包号偏移轮转：提示词现在是**逐包规划**的（services/douyin_visual.py），
    # 第 1 条就是这张图的封面，顺序有含义，偏移会把封面挪到中间。
    # 兜底路径（配置里的固定提示词池）已在 image_prompts_for 里轮换成 n 条。
    for k in range(n):
        pr = str(prompts[k % len(prompts)]).replace("{city}", city)
        try:
            ref = imagegen.generate(pr, size=size, provider=channel, model=model,
                                    salt=f"pack{pid}")
        except Exception as exc:  # noqa: BLE001 - 单张失败降级，不中断整条内容包
            log.warning("douyin_image_fallback", extra={
                "event": "douyin_image_fallback", "pack": pid, "index": k + 1,
                "err": str(exc)[:160]})
            degraded.append(k + 1)
            ref = imagegen.generate(pr, size=size, provider=imagegen.MOCK_ID,
                                    salt=f"pack{pid}")
        fn = _save_ref(pid, k + 1, ref)
        # _save_ref 下载失败时会**原样返回 http URL**：方舟的 TOS 链接约 24h 过期，
        # 存进 pack.json 意味着第二天这张配图裂图，而页面上没有任何提示。
        if str(fn).startswith(("http://", "https://")):
            url_fallback.append(k + 1)
        images.append(fn)
    return images, (list(range(1, n + 1)) if forced_mock else degraded), forced_mock, url_fallback


def _save_ref(pid: int, idx: int, ref: str) -> str:
    """把生图结果落成本地文件，返回文件名。

    两种来源都必须落盘：
    - `data:` URI：解码写文件（原有路径）
    - `http(s)` URL：方舟 Seedream 返回的是**带时效的 TOS 预签名链接**（约 24h 失效）。
      原样存进 pack.json 的话，配图会在第二天全部裂图 —— 所以必须下载后本地化。
    下载失败就退回原 URL：至少不断产线（qc 不标注的话用户看不出来，故记 warning）。
    """
    if ref.startswith("data:"):
        head, b64 = ref.split(",", 1)
        fn = f"img{idx}.{'svg' if 'svg' in head else 'png'}"
        douyin_store.save_image(pid, fn, base64.b64decode(b64))
        return fn
    if ref.startswith(("http://", "https://")):
        try:
            with net.urlopen(ref, timeout=_IMG_TIMEOUT) as resp:
                payload = resp.read()
                content_type = str(resp.headers.get("Content-Type") or "")
            ext = "png"
            if "jpeg" in content_type or "jpg" in content_type:
                ext = "jpg"
            elif "webp" in content_type:
                ext = "webp"
            fn = f"img{idx}.{ext}"
            douyin_store.save_image(pid, fn, payload)
            return fn
        except Exception as exc:  # noqa: BLE001 - 下载失败不断产线，退回原 URL
            log.warning("douyin_image_url_download_failed", extra={
                "event": "douyin_image_url_download_failed", "pack": pid,
                "index": idx, "err": str(exc)[:160]})
            return ref
    return ref


# 结构校验 + 重复查重统一在 services/douyin_quality.py（本文件接近 300 行门禁）。
# 保留 `_` 前缀别名，既有测试与调用方无需改动。
from app.services.douyin_quality import (  # noqa: E402
    copy_floor as _copy_floor,
    copy_problems as _copy_problems,
    dedup_hint as _dedup_hint,
    dup_hits as _dup_hits,
    expected_footer as _expected_footer,
    incomplete_hint as _incomplete_hint,
    samples as _samples,
    topic_angle as _topic_angle,
)


def copy_for(cfg: dict, city: str, brand: str, phone: str,
             topic: dict | None = None):
    """LLM 出图文文案（可按选题写），返回 `(title, description, tags, from_llm)`。

    兜底给中性短文案且不复述 prompt，并通过 `from_llm=False` 让调用方在 qc 里标记
    来源（历史上曾把 LLMClient 当函数调用、把 creative_prompt 当正文发出去）。
    """
    # 品牌资料必须**真的喂进去**，且指令里的占位符必须**真的被替换**：
    # `{brand_data}` `{brand}` `{city}` `{phone}` `{address}` `{banned}` 以前一个都不替换
    # （只有出图提示词的 `{city}` 被 replace 过），模型拿到的是字面量花括号 ——
    # 实测文案里出现过"品牌：{brand}"。变量表与替换统一在 douyin_vars。
    v = douyin_vars.copy_vars(cfg, city, brand, phone)
    facts = (f"【品牌资料（只能依据这里的事实，不要编造）】\n{v['brand_data']}\n\n"
             if v["brand_data"] else "")
    # 违禁词必须**进 Prompt**：页面写的是"禁用断言词（基础参数里配置，模型禁写）"，
    # 而改造前只在生成后 qc 里标一句"疑似违禁词" —— 模型压根不知道哪些词不能写。
    banned = (f"【禁用词（一律不得出现，出现即视为不合格）】{v['banned']}\n\n"
              if v["banned"] else "")
    from app.services import douyin_topics

    # 有选题就按选题写（差异化）；没有才退回单角度提示
    topic_hint = (douyin_topics.topic_block(topic)
                  or f"若上文未指定主题，优先写这个角度：{_topic_angle()}\n\n")
    prompt = (f"{douyin_vars.render(cfg.get('copy_prompt') or '', v, label='copy_prompt')}\n\n"
              f"{facts}{banned}"
              f"城市：{v['city']}；品牌：{v['brand']}；业务：{douyin_vars.business_label()}；"
              f"服务行：{v['address']}；{topic_hint}"
              "【输出格式·最高优先级】只输出一个 JSON 对象，不要代码围栏、不要任何解释文字：\n"
              '{"title":"标题(20字内)","description":"正文文案","tags":["标签","标签"]}\n'
              "description 为纯文本（不要 HTML/Markdown）。")
    # 兜底文案**必须轮换**：固定模板会让同城市产出字节级相同的内容 ——
    # 实测「苏州·上门服务实录」×11、「杭州·上门服务实录」×10 全是固定兜底堆出来的。
    lead, body = random.choice([
        ("先看清损伤类型", "断裂、暗裂、磕口适用的工艺并不相同，先分类型再谈方案"),
        ("先确认修复边界", "不是所有损伤都值得修，结构性碎裂严重或价值低于成本时要重新权衡"),
        ("先问清工序与周期", "鉴定、方案确认、工艺实施、复核交付，各环节的周期口径要先问清"),
    ])
    fallback = (f"{city}修复知识分享",
                f"{city}的修复需求，{lead}：{body}。具体以实物鉴定后的方案为准。",
                ["修复知识", "知识分享"])
    footer = _expected_footer(cfg)
    floor = _copy_floor(cfg)
    # 查重样本：既有内容包的标题 + 正文片段。没有查重时 LLM 会反复产出同一句
    # （实测「玉镯断了还能不能戴？」×12），兜底又会整批复读，必须拦。
    try:
        existing = _samples(douyin_store.list_packs())
    except Exception:  # noqa: BLE001 - 读不到库不该阻断产线（降级为不查重）
        existing = []
    try:
        from app.services.llm import build_llm_client
        from app.services.writer.output_clean import keep_question_only, strip_fences

        client = build_llm_client(current_app.config["GEO_SETTINGS"])
        cur = prompt
        # 最多 3 次：① 原稿 ② 结构不合格→带问题重写 ③ 与既有重复→带避重清单重写。
        # 仍不合格就落到兜底（并标 from_llm=False）—— 绝不把半截/重复稿当成品。
        for attempt in (1, 2, 3):
            raw = strip_fences(str(client.complete(
                [{"role": "user", "content": cur}],
                # 长文必须给足输出预算：reasoning 模型会先把预算花在思维链上，
                # 默认上限下正文常常写到一半就停（实测有 251 字的半截文案）
                max_tokens=8000) or ""))
            try:
                data = json.loads(raw)
            except ValueError:
                m = _JSON_RE.search(raw)
                data = json.loads(m.group(0)) if m else {}
            # 与文章 T 段同一口径：问号之后的"做法建议"尾巴一律砍掉。
            # 实测模型顽固沿用"前半句问法 + 后半句建议"（如"玉镯断了还能不能戴？先看断口
            # 再谈修复"），而科普问句更干净、也更像用户会真的问出口的那句话。
            # 抖音图文 title 平台只收 20 字符（imageText schema）：生成侧就截到 20，
            # 免得造出 20~30 字的标题，到发布时被 schema 整批拒（2026-10-21 实测）。
            title = keep_question_only(str(data.get("title") or "").strip())[:20]
            # **不硬截断**：copy_prompt 要求 600–900 字，而 [:900] 会在超长时把正文
            # 拦腰砍断（实测两个包正好停在 884 / 892 字）。是否截断交给结构校验判。
            desc = str(data.get("description") or "").strip()
            tags = [str(t).lstrip("#").strip()
                    for t in (data.get("tags") or []) if str(t).strip()][:8]
            problems = _copy_problems(desc, footer, floor) if title else ["缺少标题"]
            if problems:
                if attempt < 3:
                    log.warning("douyin_copy_incomplete", extra={
                        "event": "douyin_copy_incomplete", "problems": problems,
                        "chars": len(re.sub(r"\s", "", desc)), "tail": desc[-24:]})
                    cur = prompt + _incomplete_hint(problems, footer, floor)
                continue
            hits = _dup_hits(title, desc, existing)
            if hits:
                if attempt < 3:
                    log.warning("douyin_copy_duplicate", extra={
                        "event": "douyin_copy_duplicate", "title": title[:40],
                        "hits": len(hits)})
                    cur = prompt + _dedup_hint(hits)
                continue
            return title, desc, tags or ["同城服务"], True
    except Exception as exc:  # noqa: BLE001 - LLM 不可用/格式不合 → 模板兜底，产线不断流
        # 兜底不能静默：否则"正常出稿"与"上游 LLM 挂了很久"在日志里毫无区别
        log.warning("douyin_copy_llm_fallback", extra={
            "event": "douyin_copy_llm_fallback", "err": str(exc)[:120]})
    return (*fallback, False)
