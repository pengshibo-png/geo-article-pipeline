"""客户画像「保存并 AI 分析」的回归测试（2026-10-19 事故）。

事故：用户加了「核心痛点」「常见地点」等条目，点「保存并 AI 分析」后切到文章生成
再回来，条目不翼而飞。原因：`POST /api/audience/analyze` 把请求里的 `profile`
**整份丢掉**，只入队分析任务 —— 画像一个字没落库；而分析任务读的是库里的**旧画像**，
所以洞察也是按旧数据算的。
"""
from __future__ import annotations

from app.platform import jobs
from tests.conftest import login

PROFILE = {
    "singles": {"who": "30-50 岁女性，手镯多为家人赠送"},
    "lists": {"pain": ["反复修还是断", "怕被乱报价"],
              "places": ["上海静安区", "浦东新区"]},
    "customs": [],
}


def test_analyze_saves_profile_before_queueing(app, client, monkeypatch):
    """「保存并 AI 分析」必须先落库，再入队。"""
    login(client, "alpha", "pw-alpha")

    from app.blueprints.config import audience_jobs

    enq: list[str] = []
    monkeypatch.setattr(jobs, "enqueue",
                        lambda name, payload, **kw: enq.append(name))
    monkeypatch.setattr(audience_jobs, "try_begin", lambda tid: True)

    r = client.post("/api/audience/analyze", json={"profile": PROFILE})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert enq == ["audience.analyze"], "仍要入队分析任务"

    got = client.get("/api/audience").get_json()["profile"]
    assert got["lists"]["pain"] == ["反复修还是断", "怕被乱报价"], "痛点必须落库"
    assert got["lists"]["places"] == ["上海静安区", "浦东新区"], "地点必须落库"
    assert got["singles"]["who"].startswith("30-50 岁女性")


def test_analyze_without_profile_keeps_existing(app, client, monkeypatch):
    """不带 profile 的调用（旧前端/脚本）不能把已有画像清空。"""
    login(client, "alpha", "pw-alpha")

    from app.blueprints.config import audience_jobs

    monkeypatch.setattr(jobs, "enqueue", lambda *a, **kw: None)
    monkeypatch.setattr(audience_jobs, "try_begin", lambda tid: True)

    client.post("/api/audience", json={"profile": PROFILE})
    client.post("/api/audience/analyze", json={})       # 无 profile

    got = client.get("/api/audience").get_json()["profile"]
    assert got["lists"]["pain"] == ["反复修还是断", "怕被乱报价"]
