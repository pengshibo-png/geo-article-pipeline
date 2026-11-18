"""文件存储（ADR-003 §2.3 D4：路径租户化 + 防 `../` 穿越）。

文章资产契约（施工规格书 C4，**不可改名**）：
    <dir>/01_draft.html   A 段初稿
    <dir>/02_review.json  B 段审稿 JSON
    <dir>/03_final.html   C 段定稿
这三个文件同时也是**断点续跑**的依据：存在即已完成该阶段。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.errors import Forbidden, NotFound
from app.tenancy import current_tenant

DRAFT = "01_draft.html"
REVIEW = "02_review.json"
FINAL = "03_final.html"
STAGES = (DRAFT, REVIEW, FINAL)
# 定稿复评（C 段产出之后再审一次）：**不属于**资产三件套 —— checkpoint 不依赖它，
# 但允许写进文章目录，且回 A 段重写时要一并清掉。
FINAL_REVIEW = "04_final_review.json"
ASSET_FILES = STAGES + (FINAL_REVIEW,)


def _base(base: str | Path | None) -> Path:
    if base is not None:
        return Path(base)
    from flask import current_app

    return Path(current_app.config["DATA_ROOT"])


def tenant_path(*parts: str, base: str | Path | None = None) -> Path:
    """取当前租户下的路径。任何 `..` 逃逸都会被拒绝（D4）。"""
    root = (_base(base) / current_tenant()).resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        raise Forbidden("path escape")
    return target


def article_dir(dir_name: str, *, base: str | Path | None = None) -> Path:
    if not dir_name or "/" in dir_name or "\\" in dir_name or ".." in dir_name:
        raise Forbidden(f"非法文章目录名: {dir_name!r}")
    return tenant_path("articles", dir_name, base=base)


def write_article_file(dir_name: str, filename: str, content: str, *, base=None) -> Path:
    if filename not in ASSET_FILES:
        raise Forbidden(f"非法的文章文件名: {filename!r}（只允许 {ASSET_FILES}）")
    path = article_dir(dir_name, base=base) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def read_article_file(dir_name: str, filename: str, *, base=None) -> str:
    path = article_dir(dir_name, base=base) / filename
    if not path.exists():
        raise NotFound(f"文章文件不存在: {filename}")
    return path.read_text(encoding="utf-8")


def remove_article_files(dir_name: str, *filenames: str, base=None) -> list[str]:
    """删除资产三件套里的指定文件（返回实际删掉的）。

    用途：重写环回 A 段之前清掉上一轮的 `01_draft.html` / `02_review.json`。
    目录名 = 标题、跨轮不变，而 A 段按"初稿存在即跳过"做断点续跑 ——
    不清掉的话 `generate_article` 会直接返回 skipped，重写根本不会发生。

    只允许三件套里的固定文件名（复用 write_article_file 的白名单），
    不接受任意路径，因此不存在穿越面。
    """
    d = article_dir(dir_name, base=base)
    removed: list[str] = []
    for name in filenames:
        if name not in ASSET_FILES:
            raise Forbidden(f"非法的文章文件名: {name!r}（只允许 {ASSET_FILES}）")
        p = d / name
        if p.exists():
            p.unlink()
            removed.append(name)
    return removed


def checkpoint(dir_name: str, *, base=None) -> dict[str, bool]:
    """返回各阶段产物是否存在 —— 断点续跑据此决定从哪一步开始。"""
    d = article_dir(dir_name, base=base)
    return {name: (d / name).exists() for name in STAGES}


def resume_stage(dir_name: str, *, base=None) -> str:
    """断点续跑：返回下一个应执行的阶段字母（T 表示还没开始 A）。"""
    state = checkpoint(dir_name, base=base)
    if not state[DRAFT]:
        return "A"          # 无初稿 → 从 A 开始（T 属于选题库，不在文章目录）
    if not state[REVIEW]:
        return "B"
    if not state[FINAL]:
        return "C"
    return "done"


def write_json(dir_name: str, filename: str, payload: Any, *, base=None) -> Path:
    return write_article_file(dir_name, filename, json.dumps(payload, ensure_ascii=False, indent=2), base=base)
