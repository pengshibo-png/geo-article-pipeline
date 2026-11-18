"""维护模式：一键停/起服务，释放 app.db 等文件锁。

背景：看门狗（计划任务「GEO工作台看门狗」）每 5 分钟探活一次，服务不在线
就拉起 —— 于是用户想删/换 app.db 时，刚停服务它又被拉起来，文件重新被锁，
Windows 弹「操作无法完成，因为文件已在 Python 中打开」。

维护模式用标记文件 data/.maintenance 让看门狗主动不拉起，并停掉当前实例。

用法：
    python tools/maintenance.py on       进入维护：停服务 + 停看门狗（文件可操作）
    python tools/maintenance.py off      退出维护：起看门狗 + 拉起服务
    python tools/maintenance.py status   查看状态
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / ".venv" / "Scripts" / "python.exe"
FLAG = ROOT / "data" / ".maintenance"
TASK = "GEO工作台看门狗"
PORT = 7001
SERVICE_MARK = "geo-article-pipeline"


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def ps(cmd: str) -> str:
    _, out = run(["powershell", "-NoProfile", "-Command", cmd])
    return out.strip()


def stop_service() -> None:
    """停掉本项目的服务进程（含 exe 与 dev 实例）。"""
    pat = str(ROOT).replace("'", "''")
    ps(f"Get-Process -Name python,GEOArticlePipeline -ErrorAction SilentlyContinue | "
       f"Where-Object {{ $_.Path -like '*{pat}*' }} | "
       f"ForEach-Object {{ Stop-Process -Id $_.Id -Force }}")
    time.sleep(1.5)


def healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        return bool(d.get("ok")) and d.get("service") == SERVICE_MARK
    except Exception:
        return False


def task_enabled() -> bool:
    _, out = run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"])
    return "Disabled" not in out


def on() -> None:
    FLAG.parent.mkdir(parents=True, exist_ok=True)
    FLAG.write_text("maintenance on\n", encoding="utf-8")
    run(["schtasks", "/Change", "/TN", TASK, "/DISABLE"])
    stop_service()
    print("[maintenance] ON  - 服务已停、看门狗已停用，app.db 等文件现在可以随意操作")
    print(f"[maintenance] 恢复：python tools/maintenance.py off")


def off() -> None:
    if FLAG.exists():
        FLAG.unlink()
    run(["schtasks", "/Change", "/TN", TASK, "/ENABLE"])
    run(["schtasks", "/Run", "/TN", TASK])
    for _ in range(20):
        time.sleep(2)
        if healthy():
            print("[maintenance] OFF - 看门狗已启用，服务已恢复")
            return
    print("[maintenance] OFF - 看门狗已启用；服务由看门狗在下一个周期(<=5分钟)拉起")


def status() -> None:
    print(f"[maintenance] 维护模式 : {'ON（文件可操作）' if FLAG.exists() else 'OFF'}")
    print(f"[maintenance] 看门狗任务 : {'禁用' if not task_enabled() else '启用'}")
    print(f"[maintenance] 服务 {PORT} : {'在线' if healthy() else '离线'}")


if __name__ == "__main__":
    act = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if act == "on":
        on()
    elif act == "off":
        off()
    else:
        status()