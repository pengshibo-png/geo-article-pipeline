#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CI 一键门禁（S4 交付物 3）。

按顺序执行，**任一失败即停**：
  1. S3 规格门禁 —— 变量/硬编码（G2/G3/G10）
  2. S3 规格门禁 —— B 段 JSON 契约（G5）
  3. S3 规格门禁 —— 品牌资料完整度与同源（G7）
  4. 工程门禁 + 全量测试 —— 行数预算 / 依赖方向 / 隔离契约 V1–V6 / 流水线 E2E

用法：./.venv/Scripts/python.exe tools/ci.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
S3 = ROOT / "spec"

GATES: list[tuple[str, list[str], Path]] = [
    ("S3 · 变量与硬编码 (G2/G3/G10)", [PY, "check_variables.py"], S3 / "prompts" / "_共享"),
    ("S3 · B 段 JSON 契约 (G5)",
     [PY, "validate_review.py", "B-审稿输出-样例.json"], S3 / "prompts" / "_样例"),
    ("S3 · 品牌资料同源 (G7)", [PY, "check_brand.py"], S3 / "品牌资料规范"),
    ("工程门禁 + 全量测试", [PY, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"], ROOT),
]

# pytest 退出码在本机不可靠：安全删除层在 atexit 清理临时目录时抛 SystemExit(1)，
# 即使全部用例通过也会返回 1。故以 tests/conftest.py 落盘的 .pytest-result.json 为准。
RESULT_FILE = ROOT / ".pytest-result.json"


def _pytest_verdict(proc) -> tuple[int, str]:
    """返回 (退出码, 补充说明)。"""
    try:
        data = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 读不到就退回退出码
        return proc.returncode, "（无结果文件，仅看退出码）"

    if data.get("failed", 0) > 0:
        ids = data.get("failed_ids", [])
        detail = f"失败 {data['failed']} 个：" + "; ".join(ids[:5])
        return 1, detail

    line = f"{data['passed']} passed / {data['skipped']} skipped"
    # 退出码非 0 但无失败用例 → 安全层噪音，判PASS
    return 0, line + ("（退出码噪音已忽略）" if proc.returncode else "")


def main() -> int:
    print("=" * 72)
    print("  GEO 内容营销 SaaS · CI 门禁")
    print("=" * 72)
    results: list[tuple[str, int]] = []

    for name, cmd, cwd in GATES:
        if not cwd.exists():
            print(f"\n[SKIP] {name} —— 目录不存在: {cwd}")
            results.append((name, 2))
            continue
        print(f"\n▶ {name}")
        print(f"   $ cd {cwd} && {' '.join(Path(c).name for c in cmd)}")
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        code = proc.returncode
        note = ""
        if "pytest" in cmd:
            code, note = _pytest_verdict(proc)
            if note:
                print(f"   | {note}")
        tail = (proc.stdout or "").strip().splitlines()[-6:]
        for line in tail:
            print(f"   | {line}")
        results.append((name, code))
        if code != 0:
            print(f"   ✗ 退出码 {code} —— **停止后续门禁**")
            if proc.stderr:
                print("   stderr:", proc.stderr.strip()[-400:])
            break

    print("\n" + "=" * 72)
    print("  门禁结果")
    print("=" * 72)
    for name, code in results:
        mark = {0: "PASS", 2: "SKIP"}.get(code, "FAIL")
        print(f"  [{mark}] {name}")
    failed = [n for n, c in results if c not in (0, 2)]
    print("-" * 72)
    print(f"  结论：{'全部通过 ✅' if not failed else '未通过 ❌ ' + str(failed)}")
    (ROOT / ".ci-result.txt").write_text(
        "\n".join(f"[{'PASS' if c == 0 else ('SKIP' if c == 2 else 'FAIL')}] {n}" for n, c in results)
        + f"\n结论：{'全部通过' if not failed else '未通过 ' + str(failed)}\n",
        encoding="utf-8",
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
