"""核对时效变量 current_period() 的实际值（是否与系统时间一致）。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer.angles import current_period  # noqa: E402
from app.timeutil import now_local  # noqa: E402

print("系统本地时间 :", datetime.now().isoformat(sep=" ", timespec="seconds"))
print("now_local()  :", now_local().isoformat(sep=" ", timespec="seconds"))
print("current_period():", current_period())
print("期望          :", f"{datetime.now().year}年{datetime.now().month}月")
