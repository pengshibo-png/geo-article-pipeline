r"""GEO 桌面服务的看门狗：探活失败才拉起，供 Windows 计划任务定时调用。

用法（计划任务每 5 分钟一次，或手动执行）：
    .venv\Scripts\python.exe tools\watchdog.py

逻辑：
  维护模式（data/.maintenance 存在）-> 直接退出，不拉起（用户正在动文件）；
  /healthz 正常 -> 直接退出（零开销，不打扰运行中的实例）；
  不健康 / 连不上 -> 以 GEO_NO_BROWSER=1 后台拉起 desktop.launcher
  （DETACHED_PROCESS，与看门狗进程脱钩，不弹浏览器，适合无人值守）。

为什么需要它：服务进程可能被外部终止（TerminateProcess 不留任何痕迹 ——
2026-10-21 实测多次：发布成功后进程消失，app.log 无异常栈、Windows 事件日志
与 WER 均无 python 崩溃记录）。应用挂掉期间定时发布全部错过（douyin_timed
有 3 小时补发窗口，超窗即 missed），看门狗把停机窗口压到最多一个探测周期。

维护模式为什么必需：看门狗会自动把服务拉回来，于是用户想删/换 app.db 时
刚停服务它又被拉起、文件重新被锁（Windows 弹「文件正在使用」）——
用 tools/maintenance.py on 停用看门狗并停服，文件才真正可操作。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / ".venv" / "Scripts" / "python.exe"
PORT = 7001
SERVICE_MARK = "geo-article-pipeline"
DETACHED = 0x00000008 | 0x08000000        # DETACHED_PROCESS | CREATE_NO_WINDOW
MAINT_FLAG = ROOT / "data" / ".maintenance"


def healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ok")) and data.get("service") == SERVICE_MARK
    except Exception:
        return False


def main() -> int:
    if MAINT_FLAG.exists():
        return 0                      # 维护模式：用户正在动文件，别拉起
    if healthy():
        return 0
    if not EXE.exists():
        print(f"not found: {EXE}", file=sys.stderr)
        return 1
    env = dict(os.environ, GEO_NO_BROWSER="1")
    subprocess.Popen([str(EXE), "-m", "desktop.launcher"], cwd=str(ROOT),
                     env=env, creationflags=DETACHED,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("service down, launched in background (GEO_NO_BROWSER=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())