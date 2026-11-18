"""蚁小二发布的封面解析与上传（发布链路前置步骤，非可选装饰）。

蚁小二各平台 article 发布**强制封面**：schema 必填 `covers`（且每项要
`key/size/width/height`），preflight 另要 `cover`（完整上传资源对象）+
`coverKey`。资源必须经 `yxer upload` 拿到 Key，严禁直接引用外部 URL
（execution-standard §3）。

封面来源（按优先级，都不命中也有兜底，保证发布链路不断）：
  ① 文章内嵌配图（A 段自动配图注入的 `<img src="/media/...">`）
  ② 素材库首图（Asset.kind=image）
  ③ 内置纯色占位图（800×450 PNG，无第三方依赖；不写字，不做视觉承诺）
"""
from __future__ import annotations

import os
import re
import struct
import tempfile
import zlib
from pathlib import Path

from app.logging import get_logger
from app.platform import storage
from app.services.publish.drivers import PublishError
from app.services.publish.yxer_cli import run_yxer

log = get_logger(__name__)

_IMG_RE = re.compile(r'<img[^>]+src="/media/([^"]+)"', re.I)
_COVER_SIZE = (800, 450)
_COVER_RGB = (31, 78, 121)              # 中性深蓝，不绑定任何品牌色


def _media_path(rel: str) -> Path | None:
    """`/media/assets/<file>` → 租户本地文件；非法路径或越界返回 None。"""
    parts = (rel or "").split("/")
    if len(parts) != 2 or parts[0] != "assets":
        return None
    try:
        return storage.tenant_path(*parts)
    except Exception:                    # noqa: BLE001 — 路径非法/无上下文都走兜底
        return None


def resolve_cover(html: str) -> Path:
    """封面的本地文件路径（三级回退，必返回一个存在的文件）。"""
    m = _IMG_RE.search(html or "")
    if m:
        p = _media_path(m.group(1))
        if p and p.exists():
            return p
    from app.models import Asset
    from app.tenancy import TenantScopedRepository

    row = TenantScopedRepository(Asset).query().filter(Asset.kind == "image").first()
    if row is not None:
        p = _media_path(row.rel_path)
        if p and p.exists():
            return p
    return _fallback_cover()


def _fallback_cover() -> Path:
    """内置纯色封面：800×450 RGB PNG，zlib+struct 手写，零依赖。"""
    fd, name = tempfile.mkstemp(prefix="yxer-cover-", suffix=".png")
    os.close(fd)
    path = Path(name)
    path.write_bytes(_solid_png(*_COVER_SIZE, rgb=_COVER_RGB))
    return path


def _solid_png(w: int, h: int, *, rgb: tuple[int, int, int]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)          # 8bit 真彩
    raw = (b"\x00" + bytes(rgb) * w) * h
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def upload_cover(path: Path) -> dict:
    """上传封面 → 蚁小二资源对象（key/size/width/height，preflight 直接引用）。"""
    data = run_yxer(["upload", str(path), "--auto-meta"], timeout=120)
    res = data.get("data") or {}
    if not str(res.get("key") or ""):
        raise PublishError(f"蚁小二上传响应缺少 key：{str(res)[:120]}", code="upstream")
    return res


def cover_entry(res: dict) -> dict:
    """上传资源对象 → schema 要的 covers 项（key/size/width/height）。"""
    return {"key": str(res.get("key") or ""),
            "size": int(res.get("size") or 0),
            "width": int(res.get("width") or 0),
            "height": int(res.get("height") or 0)}
