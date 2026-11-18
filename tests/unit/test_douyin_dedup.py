"""抖音图文「重复作品」治理的回归测试（2026-10-19）。

实测背景：46 个内容包只有 17 个不同标题 —— LLM 反复产出同一句（零查重）、
LLM 失败时固定兜底模板整批复读、mock 占位图按 prompt 确定性出图导致跨包同图。
"""
from __future__ import annotations

import json


def _cfg(**kw):
    base = {"copy_prompt": "正文 600–900 字\n- 最后一行固定：以上内容为玉石修复知识分享，"
                           "具体修复方案以实物鉴定为准",
            "creative_prompt": "", "cities": ["上海"], "image_prompts": ["{city}科普图"],
            "image_count_min": 1, "image_count_max": 1, "image_channel": "mock"}
    base.update(kw)
    return base


def _pack(pid, title, desc):
    return {"id": pid, "title": title, "description": desc, "images": [], "tags": []}


def test_dup_hits_flags_repeated_title_and_body():
    """连续 12 字相同即判重：标题重复、正文照抄都要拦住。"""
    from app.services.douyin_quality import dup_hits, sample_of

    existing = [sample_of("玉镯断了还能不能戴？"),
                sample_of("先说结论：断裂的玉镯在断面干净、能对位的情况下可以拼接加固。")]
    assert dup_hits("玉镯断了还能不能戴？", "随便一段不同的正文", existing)
    assert dup_hits("全新的标题",
                    "先说结论：断裂的玉镯在断面干净、能对位的情况下可以拼接加固", existing)
    assert not dup_hits("翡翠手镯摔裂修复多少钱",
                        "这篇讲报价构成与勘验流程，与既有内容无关", existing)
    # 2026-10-19 放宽：标题只认**整句相同**，不再按"互相包含"判重
    # （samples 里混着正文片段样本，包含判定会误伤大量正常稿 → 3 次全拒后兜底）
    assert not dup_hits("玉镯断了还能不能戴？先看断口再谈修复", "完全不同的正文内容",
                        existing)
    # 太短的样本不判（避免误伤）
    assert not dup_hits("玉镯", "正文", ["玉镯"])


def test_copy_rejects_duplicate_then_rewrites(app, monkeypatch):
    """第一次产出与既有内容包重复 → 带避重清单重写 → 第二次不重复才采用。"""
    from app.services import douyin_pack
    from app.services.llm import LLMClient
    from app.services.llm.keypool import KeyPool
    from app.services.llm.providers import FakeProvider

    footer = "以上内容为玉石修复知识分享，具体修复方案以实物鉴定为准"
    # 本测试用较小的字数要求：floor 是从 copy_prompt 读的（600*0.8=480），
    # 夹具没必要真写 480 字；换成 200–400 字 → floor=160，更贴近单元测试体量。
    cfg = _cfg(copy_prompt=f"正文 200–400 字\n- 最后一行固定：{footer}")
    pad_dup = ("修复前的鉴定要看断面是否干净、能否对位，再看沁色是否断层，"
               "这两项决定了可焊可拼的空间。工艺上分无痕微晶融合、金缮、锔钉与补配修饰，"
               "适用损伤各不相同。周期上鉴定与方案确认各需半个工作日，复核交付后再寄回。")
    pad_new = ("报价由勘验、工艺、补配三项构成，任何一项含糊都要先问清。寄修要注意保价与"
               "录像留证，到店则要看清复核环节。质保条款写清覆盖范围与期限，不含人为二次"
               "损伤。判断手艺看案例的接缝处理与色泽过渡，而非听口头承诺。")
    body_dup = "先说结论：断裂的玉镯在断面干净、能对位的情况下可以拼接加固，先鉴定再定方案。" + pad_dup + footer
    body_new = "这篇讲报价构成：勘验、工艺、补配三项分开计费，区间以实物鉴定后为准。" + pad_new + footer
    monkeypatch.setattr(douyin_pack.douyin_store, "list_packs",
                        lambda: [_pack(1, "玉镯断了还能不能戴？", body_dup)])

    calls: list[str] = []

    def responder(messages):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return json.dumps({"title": "玉镯断了还能不能戴？", "description": body_dup,
                               "tags": ["a"]}, ensure_ascii=False)
        return json.dumps({"title": "断裂玉镯拼接报价由哪几项构成",
                           "description": body_new, "tags": ["a"]}, ensure_ascii=False)

    client = LLMClient(KeyPool(["k"]), FakeProvider(responder), max_retries=0)
    monkeypatch.setattr("app.services.llm.build_llm_client", lambda cfg: client)

    with app.app_context():
        title, desc, _tags, from_llm = douyin_pack.copy_for(
            cfg, "上海", "聚贤阁", "")

    assert from_llm is True
    assert title == "断裂玉镯拼接报价由哪几项构成", "重复稿不能被采用"
    assert len(calls) == 2, "第一次重复必须重写一次"
    assert "与既有内容重复" in calls[1], "要把避重清单喂回去"


def test_fallback_copy_is_not_a_fixed_string(app, monkeypatch):
    """LLM 不可用时的兜底文案必须**轮换**：固定模板正是「同城市整批复读」的根源。"""
    from app.services import douyin_pack
    from app.services.llm import LLMClient
    from app.services.llm.keypool import KeyPool
    from app.services.llm.providers import FakeProvider

    def boom(messages):
        raise RuntimeError("LLM 不可用")

    client = LLMClient(KeyPool(["k"]), FakeProvider(boom), max_retries=0)
    monkeypatch.setattr("app.services.llm.build_llm_client", lambda cfg: client)
    monkeypatch.setattr(douyin_pack.douyin_store, "list_packs", lambda: [])

    seen = set()
    with app.app_context():
        for _ in range(12):
            _title, desc, _t, from_llm = douyin_pack.copy_for(
                _cfg(), "杭州", "聚贤阁", "")
            assert from_llm is False
            seen.add(desc)

    assert len(seen) >= 2, f"兜底文案应至少 2 种（固定模板导致 10+ 个包完全相同）: {seen}"
    assert all("详询" not in d for d in seen), "兜底不得带电话"


def test_next_pack_id_skips_existing_dirs(app):
    """并发/孤儿残留时编号必须跳过已存在目录，否则会撞号互相覆盖。"""
    from app.services import douyin_store
    from app.tenancy import tenant_scope
    from tests.conftest import T1

    with app.app_context(), tenant_scope(T1):
        for pid in (1, 2, 5):
            (douyin_store.pack_dir(pid) / "images").mkdir(parents=True, exist_ok=True)
        nid = douyin_store.next_pack_id()
    assert nid == 6, f"应取 max+1 且跳过已存在目录，实际 {nid}"

    # 已存在 6 号时要继续往后找（旧实现会返回 6 → 撞号）
    with app.app_context(), tenant_scope(T1):
        (douyin_store.pack_dir(6) / "images").mkdir(parents=True, exist_ok=True)
        nid2 = douyin_store.next_pack_id()
    assert nid2 == 7, f"6 号已存在时应取 7，实际 {nid2}"


def test_next_pack_id_is_atomic_under_concurrency(app):
    """并发取号不得撞号 —— 旧实现只在锁内"扫目录取 max+1"，而目录要到出图
    （`save_image`）才创建：文案+图纲要跑几十秒，这窗口里两个调用方会拿到同一个
    pid，然后互相覆盖（一个包的图配另一个包的文案）。锁内建目录=占号。"""
    from concurrent.futures import ThreadPoolExecutor

    from app.services import douyin_store
    from app.tenancy import tenant_scope
    from tests.conftest import T1

    def _take(_):
        with app.app_context(), tenant_scope(T1):
            return douyin_store.next_pack_id()

    with ThreadPoolExecutor(max_workers=8) as ex:
        ids = list(ex.map(_take, range(8)))
    assert len(set(ids)) == 8, f"并发取号撞号了：{sorted(ids)}"
    with app.app_context(), tenant_scope(T1):
        # 占号目录必须真的落盘（否则下轮又会被分出去）
        assert all(douyin_store.pack_dir(i).is_dir() for i in ids)


def test_mock_image_salt_avoids_identical_packs():
    """同一个 prompt + 不同 salt → 不同占位图（否则跨包出现同一张图）。"""
    from app.services import imagegen

    a = imagegen.generate("竖版科普图：玉石特写", size="768x1024", provider="mock",
                          salt="pack1")
    b = imagegen.generate("竖版科普图：玉石特写", size="768x1024", provider="mock",
                          salt="pack2")
    assert a != b, "同 prompt 不同包应产出不同占位图"
    assert a.startswith("data:image/svg+xml") and b.startswith("data:image/svg+xml")
    # 无 salt 时保持可复现（旧行为不被破坏）
    c1 = imagegen.generate("同一句", size="768x1024", provider="mock")
    c2 = imagegen.generate("同一句", size="768x1024", provider="mock")
    assert c1 == c2, "无 salt 时仍应确定性输出"


def test_render_images_keeps_planned_order(app, monkeypatch):
    """图纲是**逐包规划**的，顺序即图序：第 1 张必须是封面，不能按包号偏移轮转。

    改造前提示词是固定池（4 条轮转），现在 services/douyin_visual 按文案规划出
    N 条，第 1 条 = 封面 —— 若还做偏移轮转，封面会被挪到中间。
    """
    from app.services import douyin_pack

    seen: list[str] = []
    real = douyin_pack.imagegen.generate

    def spy(prompt, **kw):
        seen.append(prompt)
        return real(prompt, **kw)

    monkeypatch.setattr(douyin_pack.imagegen, "generate", spy)
    monkeypatch.setattr(douyin_pack.douyin_store, "save_image", lambda *a, **k: None)

    planned = ["封面提示", "知识卡提示", "清单提示"]
    with app.app_context():
        douyin_pack._render_images(1, {"image_channel": "mock", "image_size": "768x1024"},
                                   planned, 3, "上海")
    assert seen == planned, f"必须按图纲顺序出图，实际 {seen}"
