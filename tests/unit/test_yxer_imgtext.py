"""抖音图文发布（YxerImgtextDriver / imageText 类型）+ 失败原因可追溯。

背景（2026-10-15 生产故障）：抖音图文发布 100% 失败，报
"平台 douyin_imgtext 不在蚁小二文章发布支持范围"。根因是 `douyin_jobs._publish_one`
误用文章型 `YxerDriver` —— 其平台表不含 douyin_imgtext，未调 CLI 就抛错。
本文件锁死修复：走 imageText 驱动 + 失败写原因 + 日志不再吞业务字段。

全部离线：subprocess 用假 runner，不联网、不依赖本机是否装了 yxer。
"""
from __future__ import annotations

import json
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from app.services.publish import yxer_cli
from app.services.publish.drivers import PublishError
from app.services.publish.yxer_imgtext import YxerImgtextDriver
from app.services.douyin_pack import pick_district_city, _PREFECTURE_CITIES
from app.tenancy import tenant_scope


def _png_bytes(w: int = 8, h: int = 12) -> bytes:
    """真·最小 PNG（带 IHDR）：本地探针要读宽高，假签名过不了 format/width 校验。"""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x11\x22\x33" * w) * h, 9))
            + chunk(b"IEND", b""))

# ── 蚁小二 imageText 模板（2026-10-15 从 `yxer publish init douyin imageText` 实测）──
ITEXT_TEMPLATE = {
    "ok": True, "action": "publish.init",
    "data": {"payload": {
        "action": "publish", "platforms": ["抖音"],
        "publishArgs": {"accountForms": [{
            "contentPublishForm": {"formType": "task", "declaration": 0,
                                   "title": "<title>", "description": "<description>"},
            "cover": {}, "coverKey": "", "images": [],
            "platformAccountId": "<platformAccountId>"}]},
        "publishChannel": "cloud", "publishType": "imageText"}},
}
UPLOAD_OK = {"ok": True, "action": "upload",
             "data": {"key": "cloud-publish/2026/10/img1.png", "size": 6096469,
                      "width": 1536, "height": 2720, "format": "png"}}
PUBLISH_OK = {"ok": True, "action": "publish", "data": {"task_set_id": "TSIMG0001"}}

# 抖音「定位」POI 查询结果。**形状照抄真实返回**（2026-10-19 yxer 3.2.28 实测）：
#   data.dataList[*] = {raw, yixiaoerId, yixiaoerName, yixiaoerDesc, ...}
_POI_1 = {"raw": {"poi_id": "POI_1",
                  "address_info": {"city": "上海市", "district": "黄浦区"}},
          "yixiaoerId": "POI_1", "yixiaoerName": "黄浦区某地",
          "yixiaoerDesc": "上海市黄浦区某路1号"}
_POI_2 = {"raw": {"poi_id": "POI_2",
                  "address_info": {"city": "上海市", "district": "黄浦区"}},
          "yixiaoerId": "POI_2", "yixiaoerName": "黄浦区另地",
          "yixiaoerDesc": "上海市黄浦区某路2号"}
LOCATIONS_OK = {"ok": True, "action": "locations", "version": "3.2.28",
                "data": {"dataList": [_POI_1, _POI_2]}}
LOCATIONS_EMPTY = {"ok": True, "action": "locations", "version": "3.2.28",
                   "data": {"dataList": []}}

# 抖音推荐榜返回的配乐（真实形状：raw/yixiaoerId/yixiaoerName 在顶层）
_MUSIC_1 = {"raw": {"music_id": "M1", "music_name": "轻音乐A"},
            "yixiaoerId": "M1", "yixiaoerName": "轻音乐A"}
_MUSIC_2 = {"raw": {"music_id": "M2", "music_name": "轻音乐B"},
            "yixiaoerId": "M2", "yixiaoerName": "轻音乐B"}
MUSIC_OK = {"ok": True, "action": "music", "version": "3.2.28",
            "data": {"dataList": [_MUSIC_1, _MUSIC_2]}}

# 「异地同名公司」陷阱：第一个是上海闵行的"嵊州市某公司"，第二个才真在嵊州市
_POI_FAR = {"raw": {"poi_id": "POI_FAR",
                    "address_info": {"city": "上海市", "district": "闵行区"}},
            "yixiaoerId": "POI_FAR", "yixiaoerName": "嵊州市某公司",
            "yixiaoerDesc": "上海市闵行区申虹路958弄1号"}
_POI_NEAR = {"raw": {"poi_id": "POI_NEAR",
                     "address_info": {"city": "绍兴市", "district": "嵊州市"}},
             "yixiaoerId": "POI_NEAR", "yixiaoerName": "嵊州某地",
             "yixiaoerDesc": "浙江省绍兴市嵊州市某路"}
LOCATIONS_MIXED = {"ok": True, "action": "locations", "version": "3.2.28",
                   "data": {"dataList": [_POI_FAR, _POI_NEAR]}}


def _err(code, category, message):
    return {"ok": False, "error": {"code": code, "category": category, "message": message}}


class _Acct:
    def __init__(self, platform="douyin_imgtext", yxer_id="acct-1", id="acct-1"):
        self.platform, self.yxer_id, self.id = platform, yxer_id, id


class FakeRunner:
    """记录 cmd；init 返回 imageText 模板，publish 时把 payload 存档。"""

    def __init__(self, publish_error: dict | None = None, query_payload: dict | None = None,
                 query_error: dict | None = None, music_payload: dict | None = None):
        self.calls: list[list[str]] = []
        self.payloads: list[dict] = []
        self.publish_error = publish_error
        self.query_payload = query_payload or LOCATIONS_OK
        self.query_error = query_error
        self.music_payload = music_payload or MUSIC_OK

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        text = " ".join(cmd)
        if "config" in text:
            return self._done({"ok": True, "data": {}})
        if "query music" in text:
            return self._done(self.music_payload)
        if "query" in text:
            if self.query_error is not None:
                return subprocess.CompletedProcess(
                    args=[], returncode=2, stdout="",
                    stderr=json.dumps(self.query_error, ensure_ascii=False))
            return self._done(self.query_payload)
        if "upload" in text:
            return self._done(UPLOAD_OK)
        if "init" in text:
            return self._done(ITEXT_TEMPLATE)
        if "publish" in text:
            path = cmd[-2]
            self.payloads.append(json.loads(open(path, encoding="utf-8").read()))
            if self.publish_error is not None:
                return subprocess.CompletedProcess(
                    args=[], returncode=2, stdout="",
                    stderr=json.dumps(self.publish_error, ensure_ascii=False))
            return self._done(PUBLISH_OK)
        return self._done({"ok": True, "data": {}})

    @staticmethod
    def _done(payload):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload, ensure_ascii=False), stderr="")


@pytest.fixture(autouse=True)
def _fake_cli_installed(monkeypatch):
    """桩掉 `cli_exe` 前置闸门：本文件的 subprocess 全部有替身，但 run_yxer
    会先 `which yxer`——不桩它，结果就取决于运行机器**装没装**蚁小二 CLI
    （开发机碰巧装了 → 绿；干净 CI / 新环境 → 红）。"""
    monkeypatch.setattr(yxer_cli, "cli_exe", lambda: "/usr/local/bin/yxer")


@pytest.fixture()
def runner(monkeypatch, tmp_path):
    fake = FakeRunner()
    monkeypatch.setattr(yxer_cli, "_run_subprocess", fake)
    # 指向不存在的配置 → sync_api_key 会走 set-api-key（fake 接管），不碰真实 CLI 配置
    monkeypatch.setattr(yxer_cli, "_CONFIG_PATH", tmp_path / "cli-config.json")
    return fake


@pytest.fixture()
def yxer_cfg(app, tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "model_config.json").write_text(
        json.dumps({"yxer": {"use_mock": False, "token": "k-yxer-test"}}), encoding="utf-8")


def _make_pack(app, tid="t_alpha", pid="13", images=("img1.png", "img2.png")):
    """在 DATA_ROOT 造一个带图内容包（_publish_one 的输入）。"""
    root = Path(app.config["DATA_ROOT"]) / tid / "douyin" / "packs" / pid
    (root / "images").mkdir(parents=True, exist_ok=True)
    for f in images:
        (root / "images" / f).write_bytes(_png_bytes())
    (root / "pack.json").write_text(json.dumps({
        "id": int(pid), "title": "测试图文", "description": "上门服务实录",
        "tags": ["同城服务"], "images": list(images)}, ensure_ascii=False),
        encoding="utf-8")


def _seed_account(aid: str, tid: str = "t_alpha") -> None:
    """把一个本地抖音号登记进发布池。

    发布计划（`douyin_publish.plan`）只对**发布池里的号**派单：池子外的账号
    （停用/掉线/换 Key 后失效的旧云号）一律跳过并记一条原因 —— 改造前是"传什么
    就发什么"，于是旧 Key 的号会连撞 7 次"未设置代理"。测试里要发到某个号，
    就得先真的把它登记进去。
    """
    from app.extensions import db
    from app.models import Account
    from app.timeutil import now_local

    db.session.add(Account(id=aid, tenant_id=tid, platform="douyin_imgtext",
                           name=aid, quota_date=now_local().date()))
    db.session.commit()


# ── YxerImgtextDriver：CLI 全链路 ───────────────────────────────────────────

def test_imgtext_publish_full_chain(app, runner, yxer_cfg, tmp_path):
    uploads = tmp_path / "a.png", tmp_path / "b.png"
    for u in uploads:
        u.write_bytes(_png_bytes())
    with app.app_context(), tenant_scope("t_alpha"):
        url = YxerImgtextDriver().publish(
            images=list(uploads), title="图文标题", desc="图文描述",
            tags=["同城"], account=_Acct())
    assert url == "yxer-task:TSIMG0001"
    text = " ".join(" ".join(c) for c in runner.calls)
    assert "imageText" in text, "必须走 imageText 类型，不能是 article"
    payload = runner.payloads[0]
    assert payload["publishType"] == "imageText"
    assert payload["platforms"] == ["抖音"]
    form = payload["publishArgs"]["accountForms"][0]
    assert form["platformAccountId"] == "acct-1"
    assert form["images"][0]["key"] == UPLOAD_OK["data"]["key"]
    assert form["images"][0]["format"] == "png"
    assert form["coverKey"] == UPLOAD_OK["data"]["key"]     # 首图作封面
    assert form["contentPublishForm"]["title"] == "图文标题"
    assert form["contentPublishForm"]["description"] == "图文描述"
    assert form["contentPublishForm"]["formType"] == "task"  # 模板默认值保留


def test_imgtext_requires_at_least_one_image(app, runner, yxer_cfg):
    """无配图如实报错，不白跑 CLI（抖音图文发不了纯文字）。"""
    with app.app_context(), tenant_scope("t_alpha"):
        with pytest.raises(PublishError) as ei:
            YxerImgtextDriver().publish(images=[], title="t", desc="d",
                                        tags=[], account=_Acct())
    assert "配图" in ei.value.message
    assert runner.calls == []


def test_imgtext_unsupported_platform_fails_without_cli(app, runner, yxer_cfg):
    with app.app_context(), tenant_scope("t_alpha"):
        with pytest.raises(PublishError) as ei:
            YxerImgtextDriver().publish(images=[], title="t", desc="d",
                                        tags=[], account=_Acct(platform="souhu"))
    assert ei.value.code == "unknown" and "souhu" in ei.value.message


def test_imgtext_mock_mode_stays_offline(app, tmp_path, monkeypatch):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "model_config.json").write_text(
        json.dumps({"yxer": {"use_mock": True}}), encoding="utf-8")
    monkeypatch.setattr(yxer_cli, "_run_subprocess",
                        lambda *a, **k: pytest.fail("mock 模式不应调 CLI"))
    with app.app_context(), tenant_scope("t_alpha"):
        url = YxerImgtextDriver().publish(images=[], title="演示图文", desc="d",
                                          tags=[], account=_Acct())
    assert url.startswith("https://mock-yxer.example.com/")


def test_imgtext_requires_key(app, tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "model_config.json").write_text(
        json.dumps({"yxer": {"use_mock": False}}), encoding="utf-8")
    with app.app_context(), tenant_scope("t_alpha"):
        with pytest.raises(PublishError) as ei:
            YxerImgtextDriver().publish(images=[], title="t", desc="d",
                                        tags=[], account=_Acct())
    assert ei.value.code == "login_expired" and "API Key" in ei.value.message


# ── 接线回归：douyin_jobs._publish_one 必须走 imageText ──────────────────────

def test_douyin_publish_one_uses_imageText_driver(app, runner, yxer_cfg):
    """生产故障的回归锁：修复前这里用 YxerDriver（article），必抛不在支持范围。"""
    with app.app_context(), tenant_scope("t_alpha"):
        _make_pack(app)
        from app.blueprints.generation.douyin_jobs import _publish_one

        url = _publish_one("13", "yx-acct-1")
    assert url == "yxer-task:TSIMG0001"
    text = " ".join(" ".join(c) for c in runner.calls)
    assert "imageText" in text
    assert "article" not in text, "绝不能再用文章型发布抖音图文"


# ── 抖音图文「定位（POI）」───────────────────────────────────────────────────
# 业务需求（2026-10-19）：发布抖音图文时，按本包选中的区县去抖音查一个地点，注入
# contentPublishForm.location；命中多个取第一个；查无/报错降级为「不加定位照常发」。

def _run_with_city(app, tmp_path, monkeypatch, city, fake):
    monkeypatch.setattr(yxer_cli, "_run_subprocess", fake)
    monkeypatch.setattr(yxer_cli, "_CONFIG_PATH", tmp_path / "cli-config.json")
    # POI 缓存是模块级 + 落盘的（生产上同一区县只查一次），测试里必须清掉，
    # 否则上一次运行的缓存会让本应触发的 `query locations` 被跳过。
    from app.services.publish.yxer_imgtext import (
        _MUSIC_CACHE, _POI_CACHE, _POI_CACHE_FILE)

    _POI_CACHE.clear()
    _MUSIC_CACHE.clear()
    try:
        _POI_CACHE_FILE.unlink()
    except FileNotFoundError:
        pass
    uploads = [tmp_path / "a.png", tmp_path / "b.png"]
    for u in uploads:
        u.write_bytes(_png_bytes())
    with app.app_context(), tenant_scope("t_alpha"):
        url = YxerImgtextDriver().publish(
            images=list(uploads), title="t", desc="d", tags=[],
            account=_Acct(), city=city)
    return url, fake.payloads[-1]


def test_imgtext_injects_first_location_when_city_given(app, yxer_cfg, tmp_path, monkeypatch):
    fake = FakeRunner(query_payload=LOCATIONS_OK)
    url, payload = _run_with_city(app, tmp_path, monkeypatch, "黄浦区", fake)
    assert url == "yxer-task:TSIMG0001"
    calls_text = " ".join(" ".join(c) for c in fake.calls)
    assert "query locations" in calls_text, "必须去抖音查 POI"
    cpf = payload["publishArgs"]["accountForms"][0]["contentPublishForm"]
    loc = cpf["location"]
    assert loc["data"]["yixiaoerId"] == "POI_1", "命中多个时取第一个"
    assert loc["data"]["yixiaoerName"] == "黄浦区某地"
    assert loc["isScp"] is False


def test_imgtext_no_location_when_query_empty(app, yxer_cfg, tmp_path, monkeypatch):
    fake = FakeRunner(query_payload=LOCATIONS_EMPTY)
    url, payload = _run_with_city(app, tmp_path, monkeypatch, "某县", fake)
    assert url == "yxer-task:TSIMG0001"
    cpf = payload["publishArgs"]["accountForms"][0]["contentPublishForm"]
    assert "location" not in cpf, "查无定位则不加，照常发"


def test_imgtext_degrades_when_query_fails(app, yxer_cfg, tmp_path, monkeypatch):
    fake = FakeRunner(query_error={"ok": False, "error": {
        "code": "net", "category": "network", "message": "超时"}})
    url, payload = _run_with_city(app, tmp_path, monkeypatch, "某县", fake)
    assert url == "yxer-task:TSIMG0001", "查定位失败不能拖累发布"
    cpf = payload["publishArgs"]["accountForms"][0]["contentPublishForm"]
    assert "location" not in cpf


def test_imgtext_skips_location_when_no_city(app, yxer_cfg, tmp_path, monkeypatch):
    fake = FakeRunner(query_payload=LOCATIONS_OK)
    url, payload = _run_with_city(app, tmp_path, monkeypatch, None, fake)
    assert url == "yxer-task:TSIMG0001"
    calls_text = " ".join(" ".join(c) for c in fake.calls)
    assert "query locations" not in calls_text, "无城市就不查定位"
    cpf = payload["publishArgs"]["accountForms"][0]["contentPublishForm"]
    assert "location" not in cpf


def test_poi_prefers_address_in_city_over_first(app, yxer_cfg, tmp_path, monkeypatch):
    """异地同名陷阱：首个是上海的公司，应优先选地址真在嵊州市的那个。"""
    fake = FakeRunner(query_payload=LOCATIONS_MIXED)
    url, payload = _run_with_city(app, tmp_path, monkeypatch, "嵊州市", fake)
    loc = payload["publishArgs"]["accountForms"][0]["contentPublishForm"]["location"]
    assert loc["data"]["yixiaoerId"] == "POI_NEAR", \
        "必须优先地址在嵊州市的地点，不能取异地的同名公司"


def test_poi_falls_back_to_first_when_no_address_match(app, yxer_cfg, tmp_path, monkeypatch):
    """一个地址都不匹配时，仍按用户策略取第一个（不因为过滤而变成不加定位）。"""
    fake = FakeRunner(query_payload=LOCATIONS_MIXED)
    url, payload = _run_with_city(app, tmp_path, monkeypatch, "不存在区", fake)
    loc = payload["publishArgs"]["accountForms"][0]["contentPublishForm"]["location"]
    assert loc["data"]["yixiaoerId"] == "POI_FAR"


def test_imgtext_uses_allocated_location_without_query(app, yxer_cfg, tmp_path, monkeypatch):
    """发布编排已按账号分配好 location 时，驱动直接用（不再按城市查）。"""
    fake = FakeRunner(query_payload=LOCATIONS_OK)
    monkeypatch.setattr(yxer_cli, "_run_subprocess", fake)
    monkeypatch.setattr(yxer_cli, "_CONFIG_PATH", tmp_path / "cli-config.json")
    assigned = {"data": {"yixiaoerId": "ASSIGNED", "yixiaoerName": "编排分配"},
                "isScp": False}
    uploads = [tmp_path / "a.png"]
    uploads[0].write_bytes(_png_bytes())
    with app.app_context(), tenant_scope("t_alpha"):
        url = YxerImgtextDriver().publish(
            images=list(uploads), title="t", desc="d", tags=[],
            account=_Acct(), location=assigned)
    assert url == "yxer-task:TSIMG0001"
    calls_text = " ".join(" ".join(c) for c in fake.calls)
    assert "query locations" not in calls_text, "已分配定位就不该再查"
    cpf = fake.payloads[-1]["publishArgs"]["accountForms"][0]["contentPublishForm"]
    assert cpf["location"] == assigned


def test_imgtext_injects_random_music(app, yxer_cfg, tmp_path, monkeypatch):
    """图文发布要从抖音推荐榜随机挂一首背景音乐（music 字段）。"""
    fake = FakeRunner()
    url, payload = _run_with_city(app, tmp_path, monkeypatch, "黄浦区", fake)
    assert url == "yxer-task:TSIMG0001"
    cpf = payload["publishArgs"]["accountForms"][0]["contentPublishForm"]
    assert cpf["music"]["yixiaoerId"] in ("M1", "M2")
    assert cpf["music"]["yixiaoerName"] in ("轻音乐A", "轻音乐B")
    calls = " ".join(" ".join(c) for c in fake.calls)
    assert "query music" in calls


def test_imgtext_degrades_when_music_query_fails(app, yxer_cfg, tmp_path, monkeypatch):
    """配乐查询失败不能拖垮发布：照常发、只是不带音乐。"""
    fake = FakeRunner(music_payload={"ok": True, "data": {"dataList": []}})
    url, payload = _run_with_city(app, tmp_path, monkeypatch, "黄浦区", fake)
    assert url == "yxer-task:TSIMG0001"
    cpf = payload["publishArgs"]["accountForms"][0]["contentPublishForm"]
    assert "music" not in cpf


# ── 抖音取城市：只落区/县/县级市，排除地级市 ─────────────────────────────────

def test_pick_district_city_excludes_prefecture():
    cities = ["黄浦区", "昆山市", "上海", "南京", "淳安县"]
    for _ in range(50):
        c = pick_district_city(cities)
        assert c not in _PREFECTURE_CITIES, f"{c} 是地级市，不应被选中"
        assert c in cities


def test_pick_district_city_keeps_county_level_city():
    # 县级市（昆山）算区县级，应被保留；上海/南京是地级市，应被排除
    assert "昆山市" not in _PREFECTURE_CITIES
    assert pick_district_city(["昆山市", "上海", "南京"]) == "昆山市"


def test_pick_district_city_fallback_when_only_prefecture():
    # 全是地级市时回退，不崩
    assert pick_district_city(["上海", "南京"]) in ("上海", "南京")


def test_douyin_publish_payload_shape(app, runner, yxer_cfg):
    """_publish_one 产出的 imageText payload 形状正确（标题/话题进描述）。"""
    with app.app_context(), tenant_scope("t_alpha"):
        _make_pack(app)
        from app.blueprints.generation.douyin_jobs import _publish_one

        _publish_one("13", "yx-acct-1")
    payload = runner.payloads[0]
    form = payload["publishArgs"]["accountForms"][0]
    assert form["contentPublishForm"]["title"] == "测试图文"
    # 话题拼进描述（图文惯例）
    d = form["contentPublishForm"]["description"]
    assert "上门服务实录" in d and "#同城服务" in d


def test_douyin_publish_failure_records_reason(app, monkeypatch, yxer_cfg, tmp_path):
    """失败也写原因：修复前账本只有 ok=false，用户/排查都看不出为啥失败。"""
    monkeypatch.setattr(yxer_cli, "_run_subprocess",
                        FakeRunner(publish_error=_err("YIXIAOER_REMOTE_ERR",
                                                      "remote_error", "后端 502")))
    monkeypatch.setattr(yxer_cli, "_CONFIG_PATH", tmp_path / "cli-config.json")
    with app.app_context(), tenant_scope("t_alpha"):
        _make_pack(app)
        _seed_account("yx-acct-1")
        from app.blueprints.generation.douyin_jobs import dy_publish_job, _st

        _st("t_alpha")                     # 初始化状态桥
        dy_publish_job({"pack_ids": ["13"], "account_ids": ["yx-acct-1"]})
        from app.services import douyin_store

        row = douyin_store.load_ledger()["13"][-1]
    assert row["ok"] is False
    assert "502" in str(row.get("error") or ""), "账本必须记下失败原因"


# ── 日志：业务 extra 字段不再被静默丢弃 ──────────────────────────────────────

def test_logging_keeps_business_extra_fields(capsys):
    """回归：白名单曾把 tenant/err/engine 等静默丢掉，导致查不到失败原因。"""
    from app.logging import configure_logging, get_logger

    configure_logging(level="INFO", json_output=True)
    get_logger("t").warning("douyin_autopub_failed",
                            extra={"event": "douyin_autopub_failed",
                                   "tenant": "t_x", "err": "boom-detail"})
    out = capsys.readouterr().out
    assert "boom-detail" in out and "t_x" in out and "douyin_autopub_failed" in out


# ── 上传前配图校验（2026-10-16：SVG 连撞 366 次 schema 错） ────────────────────

def test_imgmeta_probe_reads_header_metadata(tmp_path):
    """本地探针：零依赖解析文件头，拿到 schema 要的 format/width/height。"""
    from app.services.publish.imgmeta import probe

    png = tmp_path / "a.png"
    png.write_bytes(_png_bytes(1536, 2720))
    info = probe(png)
    assert info == {"format": "png", "width": 1536, "height": 2720,
                    "size": png.stat().st_size}

    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xc0" + struct.pack(">H", 17) + b"\x08"
                    + struct.pack(">HH", 34, 56) + b"\x00" * 10)
    got = probe(jpg)
    assert got["format"] == "jpg" and got["width"] == 56 and got["height"] == 34

    webp = tmp_path / "a.webp"
    webp.write_bytes(b"RIFF" + struct.pack("<I", 40) + b"WEBP" + b"VP8X"
                     + struct.pack("<I", 10) + b"\x00" * 4
                     + (639).to_bytes(3, "little") + (959).to_bytes(3, "little"))
    got = probe(webp)
    assert got["format"] == "webp" and got["width"] == 640 and got["height"] == 960

    svg = tmp_path / "a.svg"
    svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    assert probe(svg) is None, "SVG 解析不头部尺寸 → 判为不可识别"


def test_imgtext_rejects_svg_before_upload(app, runner, yxer_cfg, tmp_path):
    """SVG 不在抖音 schema 白名单：上传前就拒，别白传一批图再等最长 300s 超时。"""
    bad = tmp_path / "card.svg"
    bad.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    good = tmp_path / "ok.png"
    good.write_bytes(_png_bytes())
    with app.app_context(), tenant_scope("t_alpha"):
        with pytest.raises(PublishError) as ei:
            YxerImgtextDriver().publish(images=[bad, good], title="t", desc="d",
                                        tags=[], account=_Acct())
    assert "格式" in ei.value.message and "svg" in ei.value.message
    assert runner.calls == [], "不合规必须在调用 CLI 之前就拒"


def test_img_entry_falls_back_to_local_probe():
    """CLI 不回元数据（实测 SVG 得 width=0/format=svg）时，用本地探针兜底。"""
    from app.services.publish.yxer_imgtext import _img_entry

    local = {"format": "png", "width": 1536, "height": 2720, "size": 6096469}
    empty = {"key": "k1", "size": 0, "width": 0, "height": 0, "format": ""}
    entry = _img_entry(empty, local)
    assert entry == {"key": "k1", "size": 6096469, "width": 1536,
                     "height": 2720, "format": "png"}

    illegal = {"key": "k2", "size": 10, "width": 5, "height": 5, "format": "svg"}
    entry = _img_entry(illegal, local)
    assert entry["format"] == "png", "schema 不接受 svg，必须回落本地格式"


def test_douyin_publish_batch_skips_failed_account(app, monkeypatch, yxer_cfg,
                                                   tmp_path):
    """账号级失败后，同批次不再对它重试（否则换个包还是同一个错，白传图）。"""
    monkeypatch.setattr(yxer_cli, "_run_subprocess",
                        FakeRunner(publish_error=_err("YIXIAOER_REMOTE_ERR",
                                                      "remote_error", "后端 502")))
    monkeypatch.setattr(yxer_cli, "_CONFIG_PATH", tmp_path / "cli-config.json")
    with app.app_context(), tenant_scope("t_alpha"):
        _make_pack(app, pid="13")
        _make_pack(app, pid="14")
        _seed_account("yx-bad")
        from app.blueprints.generation.douyin_jobs import _st, dy_publish_job
        from app.services import douyin_store

        _st("t_alpha")
        dy_publish_job({"pack_ids": ["13", "14"], "account_ids": ["yx-bad"]})
        led = douyin_store.load_ledger()
    assert led["13"][-1]["ok"] is False
    assert "14" not in led, "同批次已失败的账号不应再对下一个包重试"
