"""抖音图文产线可靠性修复的回归测试（2026-10-16）。

事故现场：请求生成 3 条内容包，只有第 1 条成功，第 2、3 条报
`生图连接失败：The read operation timed out`，且磁盘上留下 3 个
「只有 images/、没有 pack.json」的孤儿目录（#29 #30 #32，共 22.9 MB）。

根因与修复：
- 生图串行且**零重试**，一张超时即整条内容包报废 → 加退避重试（本文件 1~3）；
- 单张仍失败时整条放弃 → 降级为内置占位图并在 qc 如实标注（本文件 4）；
- 中途失败不回滚，留孤儿目录并吃掉 next_pack_id 编号 → 组装异常即回滚（本文件 5）；
- 失败只进内存状态桥，重启后无从归因 → 落 WARNING 到磁盘日志（在 douyin_jobs）。
"""
from __future__ import annotations

import pytest

from app.services import douyin_pack, douyin_store, imagegen
from app.tenancy import tenant_scope
from tests.conftest import T1


@pytest.fixture()
def no_sleep(monkeypatch):
    """别真等退避：记录 sleep 的秒数供断言，不实际阻塞。"""
    slept: list[float] = []

    class _T:
        @staticmethod
        def sleep(seconds: float) -> None:
            slept.append(seconds)

    monkeypatch.setattr(imagegen, "time", _T)
    return slept


@pytest.fixture()
def image_ready(monkeypatch):
    """把生图通道伪装成"已配置的内置生图"，好让 generate() 走网络分支。"""
    monkeypatch.setattr(imagegen, "is_configured", lambda: True)
    monkeypatch.setattr(imagegen, "conf", lambda: {
        "protocol": imagegen.SENSENOVA_ID, "key": "k-test", "model": "m-test"})


def test_imagegen_retries_transient_failure(app, monkeypatch, no_sleep, image_ready):
    """瞬时超时（可重试）→ 重试一次即成功，用户看不到失败。"""
    calls = {"n": 0}

    def flaky(url, payload, key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise imagegen.ImageError("生图连接失败：The read operation timed out",
                                      retryable=True)
        return {"data": [{"b64_json": "QUJD"}]}

    monkeypatch.setattr(imagegen, "_post_images", flaky)
    with app.app_context():
        out = imagegen.generate("提示词", provider=imagegen.SENSENOVA_ID)

    assert calls["n"] == 2, "第一次读超时后必须重试"
    assert out == "data:image/png;base64,QUJD"
    assert no_sleep == [2.0], "重试前应退避一次"


def test_imagegen_does_not_retry_deterministic_rejection(app, monkeypatch,
                                                         no_sleep, image_ready):
    """401/400 这类确定性拒绝不重试 —— 重试只会白等退避、拖慢整条产线。"""
    calls = {"n": 0}

    def denied(url, payload, key):
        calls["n"] += 1
        raise imagegen.ImageError("生图 HTTP 401：invalid api key", retryable=False)

    monkeypatch.setattr(imagegen, "_post_images", denied)
    with app.app_context(), pytest.raises(imagegen.ImageError):
        imagegen.generate("提示词", provider=imagegen.SENSENOVA_ID)

    assert calls["n"] == 1, "确定性拒绝必须一次即止"
    assert no_sleep == [], "不该发生退避等待"


def test_imagegen_retries_are_bounded(app, monkeypatch, no_sleep, image_ready):
    """重试次数有上界：一直超时也要在有限次后放弃，不能无限重试。"""
    calls = {"n": 0}

    def always(url, payload, key):
        calls["n"] += 1
        raise imagegen.ImageError("生图连接失败：timed out", retryable=True)

    monkeypatch.setattr(imagegen, "_post_images", always)
    with app.app_context(), pytest.raises(imagegen.ImageError):
        imagegen.generate("提示词", provider=imagegen.SENSENOVA_ID)

    assert calls["n"] == imagegen._RETRY_ATTEMPTS
    assert len(no_sleep) == imagegen._RETRY_ATTEMPTS - 1


# ── 内容包组装 ───────────────────────────────────────────────────────────────

def _cfg(**kw) -> dict:
    base = {"image_prompts": ["图一", "图二"], "image_count_min": 2,
            "image_count_max": 2, "image_size": "64x64",
            "image_channel": imagegen.MOCK_ID, "banned_words": [],
            "_resolved": {"cities": ["杭州"], "brand": "阿尔法", "phone": "138"}}
    base.update(kw)
    return base


def test_pack_degrades_single_image_and_flags_qc(app, monkeypatch):
    """在线生图整条挂掉 → 内容包照样产出，但每张降级图都要在 qc 里标注。"""
    real_generate = imagegen.generate

    def only_mock(prompt, *, size="1024x1024", provider=None, salt=""):
        if provider != imagegen.MOCK_ID:
            raise imagegen.ImageError("生图连接失败：timed out", retryable=True)
        return real_generate(prompt, size=size, provider=imagegen.MOCK_ID, salt=salt)

    # 伪装"已配置 Key"：否则会被判为「Key 缺失 → 整批占位」，走另一条降级分支
    monkeypatch.setattr(imagegen, "is_configured", lambda: True)
    monkeypatch.setattr(imagegen, "generate", only_mock)
    # 文案必须走"AI 成功"（from_llm=True）：2026-10-19 起兜底文案会在生图前中止，
    # 本测试要验的是"在线生图失败→降级占位"，与文案来源无关，故直接给合格文案。
    monkeypatch.setattr(douyin_pack, "copy_for",
                        lambda *a, **k: ("降级测试标题", "正文内容。" * 200, ["同城"], True))
    monkeypatch.setattr(douyin_pack, "image_plan_for",
                        lambda *a, **k: (["图1提示", "图2提示"], []))
    with app.app_context(), tenant_scope(T1):
        pack = douyin_pack.build_pack(_cfg(image_channel=imagegen.SENSENOVA_ID))
        # 落盘成功，可被页面读到（不是孤儿目录）
        assert douyin_store.read_pack(pack["id"])["title"] == pack["title"]

    assert len(pack["images"]) == 2, "单张失败必须降级，而不是整条内容包报废"
    assert all(f.endswith(".svg") for f in pack["images"]), "降级图应为内置占位 SVG"
    note = [q for q in pack["qc"] if "占位图" in q]
    assert note and "1、2" in note[0], "降级必须如实标注，不能伪装成真实生成图"


def test_pack_rolls_back_when_assembly_fails(app, monkeypatch):
    """组装中途失败 → 目录回滚，不留孤儿、不吃掉编号。

    失败点放在**第二张图落盘时**：第一张已经写进目录，才能真正验证回滚
    （`copy_for` 现在排在最前，若它失败则目录尚未创建，无需回滚）。
    """
    real_save = douyin_pack._save_ref
    seen = {"n": 0}

    def boom_after_first(pid, idx, ref):
        seen["n"] += 1
        if seen["n"] > 1:
            raise RuntimeError("上游炸了")
        return real_save(pid, idx, ref)

    monkeypatch.setattr(douyin_pack, "_save_ref", boom_after_first)
    monkeypatch.setattr(douyin_pack, "copy_for",
                        lambda *a, **k: ("回滚测试标题", "正文内容。" * 200, ["a"], True))
    monkeypatch.setattr(douyin_pack, "image_plan_for",
                        lambda *a, **k: (["图1提示", "图2提示"], []))
    with app.app_context(), tenant_scope(T1):
        with pytest.raises(RuntimeError):
            douyin_pack.build_pack(_cfg(image_count_min=2, image_count_max=2))
        assert douyin_store.packs_dir().exists(), "图已写入，随后必须被回滚"
        leftover = sorted(p.name for p in douyin_store.packs_dir().glob("*") if p.is_dir())
        assert leftover == [], \
            f"失败必须回滚目录：孤儿目录会占磁盘并吃掉编号，实际残留 {leftover}"
        # 编号也必须回到 1。注意 `next_pack_id` 现在**在锁内就 mkdir 占号**（并发防撞），
        # 所以这次调用本身会留下一个 1 号占位目录 —— 它没有 pack.json，页面看不见。
        assert douyin_store.next_pack_id() == 1, "编号必须被释放"


def test_pack_aborts_before_images_when_copy_is_fallback(app, monkeypatch):
    """文案走兜底时必须在**生图之前**中止（2026-10-19）。

    否则会拿通用兜底标题去生图，产出"标题通用 + 图上串字"的低质包并被发出去
    （实测 #61 就这样发到了 11 个号）。这里不做图片 OCR、也不重绘 —— 在文案源头拦住。
    """
    from app.services import douyin_pack

    rendered = {"n": 0}

    def spy_render(*_a, **_kw):
        rendered["n"] += 1
        return ([], [], False, [])

    monkeypatch.setattr(douyin_pack, "copy_for",
                        lambda *a, **k: ("兜底标题", "兜底正文。", ["x"], False))
    monkeypatch.setattr(douyin_pack, "image_plan_for", lambda *a, **k: (["图1"], []))
    monkeypatch.setattr(douyin_pack, "_render_images", spy_render)
    with app.app_context(), tenant_scope(T1):
        with pytest.raises(RuntimeError) as ei:
            douyin_pack.build_pack(_cfg())
    assert "文案生成失败" in str(ei.value)
    assert rendered["n"] == 0, "兜底文案绝不能进入生图"


def test_copy_structure_check_detects_truncation():
    """结构校验：字数不足 / 结尾没落到固定尾行 → 判为"没写完"。

    实测（2026-10-18）：8 个包里 3 个是半截文案，共同特征是"结尾没有固定尾行"。
    """
    from app.services.douyin_pack import _copy_floor, _copy_problems, _expected_footer

    cfg = {"copy_prompt": "正文 600–900 字\n- 最后一行固定：以上内容为玉石修复知识分享，"
                          "具体修复方案以实物鉴定为准"}
    footer = _expected_footer(cfg)
    floor = _copy_floor(cfg)
    assert footer == "以上内容为玉石修复知识分享，具体修复方案以实物鉴定为准"
    assert floor == 480, "600 字的 80% —— 配置驱动，不写死"

    body = "这是一段用于测试的文案内容，用来把字数撑到合格区间。" * 30
    assert _copy_problems(body + footer, footer, floor) == []

    truncated = body + "聚贤阁不买卖玉石商品，"
    assert any("固定尾行" in p for p in _copy_problems(truncated, footer, floor))

    assert any("过短" in p for p in _copy_problems("根本没写完", footer, floor))

    # 配置里没有任何长度/尾行要求 → 不校验（不凭空发明标准）
    assert _copy_problems("短文案", "", 0) == []


def test_copy_retries_once_when_first_output_truncated(app, monkeypatch):
    """第一次给了半截 → 把问题清单喂回去重写一次 → 第二次完整就采用。"""
    import json as _json

    from app.services import douyin_pack
    from app.services.llm import LLMClient
    from app.services.llm.keypool import KeyPool
    from app.services.llm.providers import FakeProvider

    footer = "以上内容为玉石修复知识分享，具体修复方案以实物鉴定为准"
    calls: list[str] = []

    def responder(messages):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return _json.dumps({"title": "标题", "description": "写到一半就停",
                                "tags": ["a"]}, ensure_ascii=False)
        body = "这是一段用于测试的完整文案内容，用来把字数撑到合格区间。" * 25
        return _json.dumps({"title": "标题", "description": body + footer,
                            "tags": ["a"]}, ensure_ascii=False)

    client = LLMClient(KeyPool(["k-test"]), FakeProvider(responder), max_retries=0)
    monkeypatch.setattr("app.services.llm.build_llm_client", lambda cfg: client)

    with app.app_context():
        title, desc, tags, from_llm = douyin_pack.copy_for(
            {"copy_prompt": f"正文要求\n- 最后一行固定：{footer}",
             "creative_prompt": ""}, "杭州", "阿尔法珠宝", "")

    assert from_llm is True and title == "标题"
    assert len(calls) == 2, "第一次不合格必须重写一次"
    assert "上一次输出不合格" in calls[1], "要把具体问题喂回去"
    assert desc.endswith(footer)


def test_account_name_is_readable_not_raw_id(app):
    """发布后必须能看出"发到哪个号"：账本记账号名，不是 yx-xxxx 乱码。

    实测（2026-10-19）：账本里 453 条记录的 account 全是
    `yx-6aadf81ed033d36c0b7dfcd8` 这种 ID，页面 badge 上没法看。
    """
    from app.blueprints.generation.douyin_jobs import _account_name
    from app.extensions import db
    from app.models import Account
    from app.tenancy import tenant_scope
    from tests.conftest import T1

    with app.app_context(), tenant_scope(T1):
        db.session.add(Account(id="yx-test1", tenant_id=T1, platform="douyin",
                               name="测试抖音号", daily_limit=5, used_today=0,
                               quota_date=__import__("datetime").date.today()))
        db.session.commit()

        assert _account_name("yx-test1") == "测试抖音号"
        # 查不到退回 ID，不编造名字
        assert _account_name("yx-没有这个号") == "yx-没有这个号"
        # 演示模式的假账号名原样返回
        assert _account_name("yx-yx-douyin-1") == "yx-yx-douyin-1"


def test_discard_pack_is_silent_when_missing(app):
    """回滚路径上"本来就没建目录"是正常情况，不该再抛 NotFound 盖掉真实错误。"""
    with app.app_context(), tenant_scope(T1):
        douyin_store.discard_pack(999999)      # 不抛即通过
        assert douyin_store.list_packs() == []
