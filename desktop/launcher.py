"""桌面启动器 —— PyInstaller 入口（dev 态：python -m desktop.launcher）。

main() 的顺序即契约：
  ① _guard_streams()  ② pin_environment()  ③ probe_running_instance()
  ④ from app import create_app（★ 不是 from app import app —— 避免模块级重复建应用）
  ⑤ create_app("dev")  ⑥ ensure_first_run  ⑦ install_autologin  ⑧ 挂文件日志
  ⑨ waitress 守护线程 → 轮询 /healthz → 开浏览器  ⑩ join 常驻
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser


def _guard_streams() -> None:
    """--noconsole 下 stdout/stderr 是 None：logging/print 一碰就崩，必须最先执行。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


_guard_streams()                                    # ① 先于一切 import

from desktop import paths                           # noqa: E402
from desktop.portpicker import pick_free_port       # noqa: E402

SERVICE_MARK = "geo-article-pipeline"

# 与 tests/conftest.py 的 BRAND_ALPHA 同源：经验证可通过冻结规格全链路的合规样例，
# 仅 --selftest 的临时库使用。真实用户不受影响（开箱空品牌 = 诚实状态，R2 会阻塞
# 生成并提示去 /brand 页填写 —— 这是产品行为，不是缺陷）。
BRAND_SAMPLE = (
    "阿尔法珠宝，是杭州本地深耕十二年的实体珠宝修复工坊，主营断镯修复与金缮修缮。\n"
    "以下简称「阿尔法」。\n"
    "【资质与认证】\n主体名称：阿尔法珠宝\n统一社会信用代码：91330100MA0000000X\n"
    "【核心产品与服务】\n断镯修复系列：适合断裂手镯，采用无痕填充工艺。\n"
    "【核心方法】\n微晶融合无痕修复，是指用同源矿物粉料填充裂隙并分段固化的修复工艺。\n"
)


def _probe_own_instance(port: int) -> str | None:
    """首选端口上若已是本程序的旧实例 → 返回其 URL（防双开：复用，不重启）。"""
    import json
    from urllib.request import urlopen

    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:                    # 没起 / 非 JSON / 别人的服务 → 都当不存在
        return None
    return f"http://127.0.0.1:{port}" if data.get("service") == SERVICE_MARK else None


def _wait_healthy(url: str, seconds: float = 15.0) -> bool:
    from urllib.request import urlopen

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _selftest() -> int:
    """S5 全链路自检：一次性临时数据根，进程内跑 T→A→B→C（FakeProvider，离线）。

    冻结态下能跑通即端到端证明 geo_spec 捆绑正确 —— B 段要动态执行捆进包里的
    validate_review.py，任何规格缺漏都会在这里暴露。
    """
    import shutil
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="geo-selftest-"))
    os.environ["GEO_DATA_ROOT"] = tmp.as_posix()
    os.environ["GEO_DATABASE_URL"] = "sqlite:///" + (tmp / "selftest.db").as_posix()
    os.environ["GEO_LLM_PROVIDER"] = "fake"         # 自检必须确定性离线

    from app import create_app
    from app.platform.jobs import run_job
    from app.tenancy import tenant_scope
    from desktop.bootstrap import ensure_first_run

    app = create_app("dev")
    ensure_first_run(app)
    try:
        # R2 冻结规则：品牌概述首句必须可识别主名，否则 A 段阻塞。自检种一份与
        # tests/conftest.py 同源的合规样例（临时库用后即弃）；真实用户则在
        # /brand 页填自己的资料 —— 开箱空品牌即诚实状态。
        with app.app_context(), tenant_scope("local"):
            from app.extensions import db
            from app.models import Brand

            brand = db.session.get(Brand, "local")
            brand.content = BRAND_SAMPLE
            db.session.commit()          # 会话随上下文回收，跨块前显式提交
        with app.app_context():
            out = run_job("article.pipeline", {
                "_tenant_id": "local",
                "title": "GEO 桌面版自检：全链路冒烟",
                "via": "selftest",
                "base": tmp.as_posix(),
            })
        statuses = [s.get("status") for s in out["stages"]]
        from app.platform import storage

        with tenant_scope("local"):
            article_dir = storage.article_dir(out["dir"], base=tmp.as_posix())
        names = {p.name for p in article_dir.iterdir()}
        required = {"01_draft.html", "02_review.json", "03_final.html"}
        ok = statuses == ["drafted", "reviewed", "final"] and required <= names
        print(f"[selftest] stages={statuses} files={sorted(names)} -> "
              f"{'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    except Exception as exc:                        # 冻结态也要把失败说清楚
        print(f"[selftest] FAIL: {exc!r}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _attach_file_log(app) -> None:
    """noconsole 下 stdout 不可见：同一批记录落文件（JSON 行，含 request_id）。"""
    import logging
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    from app.logging import JsonFormatter, _ContextFilter

    logs = Path(app.config["DATA_ROOT"]) / "logs"   # 以 app 配置为准（与种子同源）
    handler = RotatingFileHandler(logs / "app.log", maxBytes=5 * 1024 * 1024,
                                  backupCount=5, encoding="utf-8")
    handler.addFilter(_ContextFilter())             # 与 configure_logging 同一过滤器
    handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(handler)


def main() -> None:
    if "--selftest" in sys.argv:
        paths.pin_environment()
        sys.exit(_selftest())

    paths.pin_environment()                         # ② chdir + 桌面环境钉死
    port = pick_free_port()
    if port != 7001:                                # ③ 首选端口被占 → 可能是旧实例
        reused = _probe_own_instance(7001)
        if reused:
            webbrowser.open(reused)
            print(f"已有运行中的实例：{reused}（已在浏览器打开）")
            return

    from app import create_app                      # ④ 环境钉好之后才 import
    app = create_app("dev")                         # ⑤ dev 配置（无 prod secret 校验）

    from desktop.autologin import install_autologin
    from desktop.bootstrap import ensure_first_run

    ensure_first_run(app)                           # ⑥ 幂等首启引导
    install_autologin(app)                          # ⑦ 抢在 tenancy 钩子之前
    _attach_file_log(app)                           # ⑧ logs/ 已由 ⑥ 建好

    # ⑧½ 定时计划执行器（桌面单实例部署：/api/autosched 的 title/article/publish 计划）
    from app.services.scheduler import start as start_scheduler

    start_scheduler(app, interval=60)

    url = f"http://127.0.0.1:{port}"
    from waitress import create_server

    server = create_server(app, host="127.0.0.1", port=port, threads=8)
    worker = threading.Thread(target=server.run, name="geo-waitress", daemon=True)
    worker.start()
    if not _wait_healthy(url):                      # ⑨
        print(f"服务启动失败，详见日志：{paths.logs_dir() / 'app.log'}")
        sys.exit(1)
    # 看门狗（tools/watchdog.py）拉起时带 GEO_NO_BROWSER=1：无人值守自动恢复
    # 不应该反复弹浏览器窗口；用户手动启动时保持原行为（自动打开工作台）。
    if os.environ.get("GEO_NO_BROWSER") != "1":
        webbrowser.open(url)
    print(f"GEO 工作台已启动：{url}（关闭本窗口或 Ctrl+C 退出）")
    import logging

    logging.getLogger(__name__).info(f"desktop serving at {url}",
                                     extra={"event": "desktop_started"})
    try:
        worker.join()                               # ⑩ 常驻（LLM 调用有界，atexit 收尾）
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
