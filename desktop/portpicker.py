"""端口选择。Windows 语义：bind 前若设 SO_REUSEADDR，别人占着的端口也会"可绑"，
探测必假阳性 —— 所以这里裸 bind，不设任何选项。"""
from __future__ import annotations

import os
import socket


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_free_port(preferred: int = 7001, tries: int = 50) -> int:
    """GEO_PORT 显式指定时直接信任（被占则由启动报错暴露，尊重用户意图）；
    否则从 preferred 起向上探测 tries 个，全忙则交由内核分配（bind 0）。"""
    env = os.environ.get("GEO_PORT", "").strip()
    if env.isdigit():
        return int(env)
    for port in range(preferred, preferred + tries):
        if _bindable(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
