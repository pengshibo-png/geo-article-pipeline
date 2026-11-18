"""把 pytest 结果写入 `.pytest-result.json`。

**为什么需要这个**：本机安全删除层在 pytest 退出时抛 `SystemExit`（atexit
清理临时目录被拦），stdout 的汇总行会一起丢失。改为在会话结束钩子里
落盘结构化结果，让 CI 与人工都能读到权威数字。

注意：本文件以 `_` 开头，**pytest 不会自动收集**，需在 `pytest.ini` 里
用 `python_files` 显式引入，或由 `tools/ci.py` 主动调用。
"""
from __future__ import annotations

import json
import pathlib

_OUT = pathlib.Path(__file__).resolve().parents[1] / ".pytest-result.json"


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    tr = getattr(session, "terminalreporter", None)
    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    if tr is not None:
        passed = [r.nodeid for r in tr.stats.get("passed", []) if r.when == "call"]
        failed = [r.nodeid for r in tr.stats.get("failed", []) if r.when == "call"]
        skipped = [r.nodeid for r in tr.stats.get("skipped", [])]

    _OUT.write_text(
        json.dumps(
            {
                "exitstatus": int(exitstatus),
                "total": len(passed) + len(failed) + len(skipped),
                "passed": len(passed),
                "failed": len(failed),
                "skipped": len(skipped),
                "failed_ids": failed,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
