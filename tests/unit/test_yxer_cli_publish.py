"""蚁小二 CLI 发布集成（yxer_cli / yxer_cover / YxerDriver）。

全部离线：subprocess 用假 runner 替身，不联网、不依赖本机是否装了 yxer。
覆盖：信封解析与错误码归一、API Key 同步（随时切换）、payload 组装
（平台模板 + 封面 + 正文）、封面三级回退、账号列表映射、mock 模式保持。
"""
from __future__ import annotations

import json
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from app.errors import NotFound
from app.services.publish import yxer, yxer_cli, yxer_cover
from app.services.publish.drivers import PublishError
from app.services.publish.yxer import YxerDriver
from app.tenancy import tenant_scope

HTML = "<h2>测试标题</h2><p>阿尔法珠宝是杭州的珠宝修复工坊，主营断镯修复。</p>"


class _Acct:
    def __init__(self, platform="souhu", yxer_id="yx-acct-1", id="yx-acct-1"):
        self.platform, self.yxer_id, self.id = platform, yxer_id, id


# ── 假 runner：按命令分派，记录调用 ─────────────────────────────────────────

INIT_TEMPLATE = {
    "ok": True, "action": "publish.init",
    "data": {"payload": {
        "action": "publish", "desc": "<desc>", "platforms": ["搜狐号"],
        "publishArgs": {"accountForms": [{
            "contentPublishForm": {"formType": "task", "pubType": 1,
                                   "title": "<title>", "desc": "<desc>", "covers": []},
            "platformAccountId": "<platformAccountId>"}],
            "content": "<content>"},
        "publishChannel": "cloud", "publishType": "article"}},
}
UPLOAD_OK = {"ok": True, "action": "upload",
             "data": {"key": "cloud-publish/2026/10/xx/cover.png", "size": 1234,
                      "width": 800, "height": 450, "contentType": "image/png",
                      "bucket": "cloud-publish", "format": "png"}}
PUBLISH_OK = {"ok": True, "action": "publish", "data": {"task_set_id": "TS12345678"}}


def _err(code, category, message, details=None):
    return {"ok": False, "error": {"type": category, "code": code,
                                   "category": category, "message": message,
                                   "details": details}}


class FakeRunner:
    """记录 cmd 列表；按内容返回预置响应。publish 调用顺带把 payload 存档。"""

    def __init__(self, responses=None):
        self.calls: list[list[str]] = []
        self.payloads: list[dict] = []
        self.responses = responses or {}

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        text = " ".join(cmd)
        if "config get" in text:
            return self._done({"ok": True, "data": {"configPath": str(yxer_cli._CONFIG_PATH or "")}})
        if "set-api-key" in text:
            return self._done({"ok": True, "data": {"apiKeyPresent": True}})
        if "publish init" in text or ("publish" in text and "init" in text):
            return self._done(INIT_TEMPLATE)
        if "upload" in text:
            return self._done(UPLOAD_OK)
        if "publish" in text:
            path = cmd[-2]
            self.payloads.append(json.loads(open(path, encoding="utf-8").read()))
            return self._done(PUBLISH_OK)
        if "accounts" in text:
            return self._done({"ok": True, "data": []})
        return self._done(_err("YIXIAOER_USAGE_ERR", "usage_error", "unexpected command"))

    @staticmethod
    def _done(payload):
        return subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout=json.dumps(payload, ensure_ascii=False),
                                           stderr="")


@pytest.fixture()
def runner(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(yxer_cli, "_run_subprocess", fake)
    monkeypatch.setattr(yxer_cli, "_CONFIG_PATH", None)
    return fake


@pytest.fixture()
def yxer_cfg(app, tmp_path):
    """把蚁小二配置写进测试 DATA_ROOT（use_mock=false + token）。"""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "data" / "model_config.json"
    cfg.write_text(json.dumps({"yxer": {"use_mock": False, "token": "k-yxer-test"}},
                              ensure_ascii=False), encoding="utf-8")
    return cfg


def _payload_file_of(fake: FakeRunner) -> dict:
    """publish 调用时存档的 payload（临时文件用完即删，只能在调用时取）。"""
    assert fake.payloads, "没有发生 publish 调用"
    return fake.payloads[0]


# ── run_yxer：信封解析与错误码归一 ─────────────────────────────────────────

def test_run_yxer_returns_ok_data(runner):
    out = yxer_cli.run_yxer(["accounts", "list"])
    assert out["ok"] is True


def test_run_yxer_maps_auth_error(runner):
    runner.responses = {}
    import app.services.publish.yxer_cli as m
    orig = m._run_subprocess

    def auth_fail(cmd, **kw):
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(_err("YIXIAOER_AUTH_ERR", "auth_error", "登录失效, 请重新登录")),
            stderr="")

    m._run_subprocess = auth_fail
    try:
        with pytest.raises(PublishError) as ei:
            yxer_cli.run_yxer(["accounts", "list"])
    finally:
        m._run_subprocess = orig
    assert ei.value.code == "login_expired"


def test_run_yxer_maps_account_not_found(runner):
    import app.services.publish.yxer_cli as m
    orig = m._run_subprocess

    def acct_fail(cmd, **kw):
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(_err("YIXIAOER_USAGE_ERR", "validation_error",
                                   "Account preflight failed",
                                   "account demo-1: not found in target platform account list")),
            stderr="")

    m._run_subprocess = acct_fail
    try:
        with pytest.raises(PublishError) as ei:
            yxer_cli.run_yxer(["publish", "article", "搜狐号", "x.json"])
    finally:
        m._run_subprocess = orig
    assert ei.value.code == "login_expired"


def test_run_yxer_maps_remote_error(runner):
    import app.services.publish.yxer_cli as m
    orig = m._run_subprocess

    def remote_fail(cmd, **kw):
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(_err("YIXIAOER_REMOTE_ERR", "remote_error", "后端 502")),
            stderr="")

    m._run_subprocess = remote_fail
    try:
        with pytest.raises(PublishError) as ei:
            yxer_cli.run_yxer(["publish", "article", "搜狐号", "x.json"])
    finally:
        m._run_subprocess = orig
    assert ei.value.code == "upstream"


def test_run_yxer_raises_when_cli_missing(monkeypatch):
    monkeypatch.setattr(yxer_cli.shutil, "which", lambda name: None)
    with pytest.raises(PublishError) as ei:
        yxer_cli.run_yxer(["accounts", "list"])
    assert ei.value.code == "timeout" and "CLI" in ei.value.message


def test_run_yxer_raises_on_non_json(runner):
    import app.services.publish.yxer_cli as m
    orig = m._run_subprocess

    def junk(cmd, **kw):
        return subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout="not json at all", stderr="")

    m._run_subprocess = junk
    try:
        with pytest.raises(PublishError) as ei:
            yxer_cli.run_yxer(["accounts", "list"])
    finally:
        m._run_subprocess = orig
    assert ei.value.code == "upstream"


def test_run_yxer_reads_error_envelope_from_stderr(runner):
    """实测回归：CLI 的错误信封走 **stderr**（rc=2），stdout 为空 —— 只解 stdout
    会把所有失败误判成"输出非 JSON"（2026-10-14 真实联调撞出来的坑）。"""
    import app.services.publish.yxer_cli as m
    orig = m._run_subprocess

    def err_on_stderr(cmd, **kw):
        return subprocess.CompletedProcess(
            args=[], returncode=2, stdout="",
            stderr=json.dumps(_err("YIXIAOER_USAGE_ERR", "validation_error",
                                   "Account preflight failed",
                                   "account demo-1: not found in target platform account list")),
            )

    m._run_subprocess = err_on_stderr
    try:
        with pytest.raises(PublishError) as ei:
            yxer_cli.run_yxer(["publish", "article", "搜狐号", "x.json"])
    finally:
        m._run_subprocess = orig
    assert ei.value.code == "login_expired"
    assert "Account preflight" in ei.value.message


def test_probe_api_key_reads_envelope_from_stderr(monkeypatch, tmp_path):
    """Key 失效（401）的信封同样在 stderr —— probe 也要能解。"""
    import app.services.publish.yxer_cli as m

    monkeypatch.setattr(m.shutil, "which", lambda name: "yxer")
    monkeypatch.setattr(m, "_run_subprocess", lambda cmd, **kw: subprocess.CompletedProcess(
        args=[], returncode=2, stdout="",
        stderr=json.dumps(_err("YIXIAOER_REMOTE_ERR", "remote_error", "登录失效, 请重新登录"))))
    out = m.probe_api_key("bad-key")
    assert out["ok"] is False and "登录失效" in out["error"]


# ── API Key 同步（随时切换） ───────────────────────────────────────────────

def test_sync_api_key_writes_when_different(runner, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"apiKey": "old-key"}), encoding="utf-8")
    yxer_cli._CONFIG_PATH = cfg
    switched = yxer_cli.sync_api_key("new-key")
    assert switched is True
    assert any("set-api-key" in " ".join(c) and "new-key" in " ".join(c)
               for c in runner.calls)


def test_sync_api_key_noop_when_same(runner, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"apiKey": "same-key"}), encoding="utf-8")
    yxer_cli._CONFIG_PATH = cfg
    assert yxer_cli.sync_api_key("same-key") is False
    assert not any("set-api-key" in " ".join(c) for c in runner.calls)


def test_sync_api_key_requires_key(runner):
    with pytest.raises(PublishError) as ei:
        yxer_cli.sync_api_key("")
    assert ei.value.code == "login_expired"


# ── YxerDriver.publish：CLI 全链路 ─────────────────────────────────────────

def test_publish_returns_task_reference(app, runner, yxer_cfg):
    with app.app_context(), tenant_scope("t_alpha"):
        url = YxerDriver().publish(html=HTML, title="测试标题甲", account=_Acct())
    assert url == "yxer-task:TS12345678"
    payload = _payload_file_of(runner)
    assert payload["platforms"] == ["搜狐号"] and payload["publishType"] == "article"
    assert payload["publishArgs"]["content"] == HTML
    form = payload["publishArgs"]["accountForms"][0]
    assert form["platformAccountId"] == "yx-acct-1"
    assert form["contentPublishForm"]["title"] == "测试标题甲"
    assert form["contentPublishForm"]["formType"] == "task"      # 平台模板默认值保留
    assert form["contentPublishForm"]["covers"][0]["width"] == 800
    assert payload["publishArgs"]["coverKey"] == UPLOAD_OK["data"]["key"]


def test_publish_syncs_key_before_calling(app, runner, yxer_cfg, tmp_path):
    cfg = tmp_path / "cli-config.json"
    cfg.write_text(json.dumps({"apiKey": "stale"}), encoding="utf-8")
    yxer_cli._CONFIG_PATH = cfg
    with app.app_context(), tenant_scope("t_alpha"):
        YxerDriver().publish(html=HTML, title="t", account=_Acct())
    assert any("set-api-key" in " ".join(c) and "k-yxer-test" in " ".join(c)
               for c in runner.calls)


def test_publish_unsupported_platform_fails_without_cli(app, runner, yxer_cfg):
    with app.app_context(), tenant_scope("t_alpha"):
        with pytest.raises(PublishError) as ei:
            YxerDriver().publish(html=HTML, title="t", account=_Acct(platform="meipian"))
    assert ei.value.code == "unknown" and "meipian" in ei.value.message
    assert runner.calls == []                     # 不白跑 CLI


def test_publish_mock_mode_stays_offline(app, tmp_path, monkeypatch):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "model_config.json").write_text(
        json.dumps({"yxer": {"use_mock": True}}), encoding="utf-8")
    monkeypatch.setattr(yxer_cli, "_run_subprocess",
                        lambda *a, **k: pytest.fail("mock 模式不应调 CLI"))
    with app.app_context(), tenant_scope("t_alpha"):
        url = YxerDriver().publish(html=HTML, title="演示标题", account=_Acct())
    assert url.startswith("https://mock-yxer.example.com/")


def test_publish_requires_key(app, tmp_path, monkeypatch):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "model_config.json").write_text(
        json.dumps({"yxer": {"use_mock": False}}), encoding="utf-8")
    with app.app_context(), tenant_scope("t_alpha"):
        with pytest.raises(PublishError) as ei:
            YxerDriver().publish(html=HTML, title="t", account=_Acct())
    assert ei.value.code == "login_expired" and "API Key" in ei.value.message


# ── 封面三级回退 ───────────────────────────────────────────────────────────

def test_cover_prefers_article_image(app, tmp_path):
    rel = tmp_path / "data" / "t_alpha" / "assets" / "pic.png"
    rel.parent.mkdir(parents=True, exist_ok=True)
    rel.write_bytes(b"fake-png")
    with app.app_context(), tenant_scope("t_alpha"):
        cover = yxer_cover.resolve_cover('<p>x</p><img src="/media/assets/pic.png">')
    assert cover == rel


def test_cover_falls_back_to_first_asset(app):
    from app.extensions import db
    from app.models import Asset

    with app.app_context():
        with tenant_scope("t_alpha"):
            p = Path(app.config["DATA_ROOT"]) / "t_alpha" / "assets"
            p.mkdir(parents=True, exist_ok=True)
            (p / "from-library.png").write_bytes(b"fake-png")
            db.session.add(Asset(kind="image", rel_path="assets/from-library.png"))
            db.session.commit()
            cover = yxer_cover.resolve_cover("<p>无图正文</p>")
            assert cover is not None and cover.name == "from-library.png"


def test_cover_falls_back_to_generated_png(app):
    with app.app_context(), tenant_scope("t_alpha"):
        p = yxer_cover.resolve_cover("<p>纯文字</p>")
    assert p is not None and p.exists() and p.stat().st_size > 100
    head = p.read_bytes()[:33]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", head[16:24])
    assert (w, h) == (800, 450)


def test_upload_cover_returns_resource(runner):
    out = yxer_cover.upload_cover(yxer_cover._fallback_cover())
    assert out["key"] == UPLOAD_OK["data"]["key"] and out["width"] == 800


def test_upload_cover_rejects_missing_key(runner):
    import app.services.publish.yxer_cli as m
    orig = m._run_subprocess
    m._run_subprocess = lambda cmd, **kw: subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"ok": True, "data": {}}), stderr="")
    try:
        with pytest.raises(PublishError) as ei:
            yxer_cover.upload_cover(yxer_cover._fallback_cover())
    finally:
        m._run_subprocess = orig
    assert ei.value.code == "upstream"


# ── list_accounts：CLI 形状映射 ────────────────────────────────────────────

def test_list_accounts_maps_cli_shape(app, yxer_cfg, runner):
    runner.responses = {}
    import app.services.publish.yxer_cli as m
    orig = m._run_subprocess

    def accts(cmd, **kw):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps({
            "ok": True, "data": [
                {"id": "a-1", "platformAccountName": "搜狐主力号",
                 "platformName": "搜狐号", "status": 1},
                {"id": "a-2", "platformAccountName": "百家小号",
                 "platformName": "百家号", "status": 2},
            ]}), stderr="")

    m._run_subprocess = accts
    try:
        with app.app_context():
            out = yxer.list_accounts()
    finally:
        m._run_subprocess = orig
    assert out["configured"] is True
    by_id = {a["id"]: a for a in out["accounts"]}
    assert by_id["a-1"]["platform"] == "souhu" and by_id["a-1"]["online"] is True
    assert by_id["a-2"]["platform"] == "baijiahao" and by_id["a-2"]["online"] is False
    assert by_id["a-1"]["name"] == "搜狐主力号"


def test_list_accounts_error_is_reported(app, yxer_cfg, runner):
    import app.services.publish.yxer_cli as m
    orig = m._run_subprocess
    m._run_subprocess = lambda cmd, **kw: subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps(_err("YIXIAOER_REMOTE_ERR", "remote_error", "502")), stderr="")
    try:
        with app.app_context():
            out = yxer.list_accounts()
    finally:
        m._run_subprocess = orig
    assert out["configured"] is True and out["accounts"] == [] and "502" in out["error"]
