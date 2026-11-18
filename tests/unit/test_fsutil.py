"""remove_tree 回归：安全删除层拒绝整体回收时，必须退化为逐文件删除。

背景（2026-10-16 实测）：删除抖音作品库/视频库时出现 500 internal_error，
根因是环境注入的"安全删除层"对混合内容目录（pack.json + images/*.svg）
拒绝回收并抛 OSError。若不兜底，用户点删除就直接 500。
"""
from __future__ import annotations

import shutil

from app.platform.fsutil import remove_tree


def _make_mixed_pack(base):
    d = base / "7"
    (d / "images").mkdir(parents=True)
    (d / "pack.json").write_text("{}", encoding="utf-8")
    for i in (1, 2, 3):
        (d / "images" / f"img{i}.svg").write_text("<svg/>", encoding="utf-8")
    return d


def test_remove_tree_normal(tmp_path):
    d = _make_mixed_pack(tmp_path)
    remove_tree(d)
    assert not d.exists()


def test_remove_tree_falls_back_when_recursive_trash_fails(tmp_path, monkeypatch):
    """整体回收被拒 → 自底向上逐个删，仍然必须删干净。"""
    d = _make_mixed_pack(tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("[safe-delete] trash aborted")

    monkeypatch.setattr(shutil, "rmtree", _boom)   # 模拟安全删除层拒绝回收
    remove_tree(d)
    assert not d.exists(), "整体回收失败后没有退化为逐文件删除"


def test_remove_tree_ignore_errors_swallows_failure(tmp_path, monkeypatch):
    d = _make_mixed_pack(tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("both paths fail")

    monkeypatch.setattr(shutil, "rmtree", _boom)
    monkeypatch.setattr("app.platform.fsutil._bottom_up", _boom)
    remove_tree(d, ignore_errors=True)             # 不应抛出
    assert d.exists()


def test_remove_tree_missing_path_is_noop(tmp_path):
    remove_tree(tmp_path / "not-there")            # 不应抛出
