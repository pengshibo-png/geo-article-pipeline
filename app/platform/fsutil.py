"""文件系统工具：**稳健的目录删除**。

为什么需要它（实测，2026-10-16）：
某些环境会在解释器里注入"安全删除层"，把 `shutil.rmtree` / `os.remove` /
`Path.unlink` 全部重定向到"移到回收站"。该层对**混合内容目录**（如
`pack.json` + `images/*.svg` 这种"一个文件 + 一个子目录里若干小文件"）
会拒绝回收并抛 `OSError`（`Error during a trash operation: Unknown
{ description: "Some operations were aborted" }`），导致上层把用户操作
暴露成 500 `internal_error`。

实测结论：
- 单层目录（只有 .txt / .svg / .json）→ 整体回收 OK
- 嵌套子目录（只有 .txt）→ 整体回收 OK
- 混合内容目录（.json + images/*.svg）→ **整体回收失败**
- 逐个文件 `unlink()` + 自底向上 `rmdir()` → **全部成功**

所以：先试整体回收（快、能进回收站、可恢复），失败再退化为自底向上逐个删。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)


def remove_tree(path, *, ignore_errors: bool = False) -> None:
    """删除目录树：优先整体删除，失败则自底向上逐个删。

    `ignore_errors=True` 时任何失败都不抛出（用于 finally 里的临时目录清理）。
    """
    p = Path(path)
    if not p.exists():
        return

    try:
        shutil.rmtree(p)
        return
    except OSError as exc:
        # 这是**预期内的降级**（安全删除层拒绝整体回收），不是故障：
        # 不带堆栈，只留一行可诊断的记录，避免把正常路径渲染成报错。
        log.warning("整目录删除被拒，改用逐文件删除",
                    extra={"event": "remove_tree_fallback", "path": str(p),
                           "err": str(exc)[:160]})

    try:
        _bottom_up(p)
    except OSError:
        if ignore_errors:
            return
        raise


def _bottom_up(p: Path) -> None:
    """先删文件、再自底向上删目录（安全删除层拒绝整体回收时的兜底）。"""
    dirs: list[Path] = []
    for item in sorted(p.rglob("*"), key=lambda x: -len(x.parts)):
        if item.is_symlink() or not item.is_dir():
            item.unlink(missing_ok=True)
        else:
            dirs.append(item)
    for d in dirs:
        d.rmdir()
    p.rmdir()


__all__ = ["remove_tree"]
