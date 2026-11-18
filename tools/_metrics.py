# -*- coding: utf-8 -*-
"""收集工程度量，供《S4 进度与门禁报告》引用（不参与门禁）。"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"


def lines_of(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def rel(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def create_app_lines() -> int:
    src = (APP / "__init__.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(src) if line.startswith("def create_app"))
    end = len(src)
    for i in range(start + 1, len(src)):
        if src[i] and not src[i][0].isspace():
            end = i
            break
    return end - start


def main() -> None:
    py = sorted(((rel(p), lines_of(p)) for p in APP.rglob("*.py")), key=lambda x: -x[1])
    total = sum(n for _, n in py)
    print(f"app.py 行数            : {lines_of(ROOT / 'app.py')}  (上限 60)")
    print(f"create_app 行数        : {create_app_lines()}  (上限 120)")
    print(f"app/ .py 文件数        : {len(py)}   总行数: {total}")
    print(f"最大单文件             : {py[0][0]} = {py[0][1]} 行  (上限 300)")
    print("Top 6 最大文件         :")
    for name, n in py[:6]:
        print(f"    {name:44s} {n:4d}")
    print(f"蓝图目录数             : {len([d for d in (APP / 'blueprints').iterdir() if d.is_dir()])}")
    print(f"模型模块数             : {len(list((APP / 'models').glob('*.py')))}")
    print(f"迁移脚本               : {[p.name for p in (ROOT / 'alembic' / 'versions').glob('*.py')]}")
    print("测试文件               :")
    for p in sorted((ROOT / "tests").rglob("test_*.py")):
        print(f"    {rel(p)}")
    print("工具脚本               :")
    for p in sorted((ROOT / "tools").glob("*.py")):
        print(f"    {rel(p)}")


if __name__ == "__main__":
    main()
