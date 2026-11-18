"""图片元数据本地探针：零依赖，直接解析文件头取 格式 / 宽 / 高。

为什么需要（实测，2026-10-16）：
抖音图文（imageText）的 schema 要求 `images[]` 每项
`format ∈ [jpg jpeg png webp]` 且 `width / height >= 1`。原做法是先把图
上传给蚁小二、再等它报 schema 错 —— 上传 + 最长 300s 超时后才失败，
白耗配额（账本实测：SVG 占位图时期连撞 366 次
`/images/0/width: must be >= 1` + `format: must be one of [jpg jpeg png webp]`）。

改为**上传前本地探针**：不合规直接拒，并告诉用户怎么修。

先只读前 64 字节（PNG/GIF/WebP 的尺寸就在这个范围内）；**JPEG 例外** ——
它的 SOF 段可能远在几十~上百 KB 之后（前面挂着 EXIF/XMP 等大段），
所以魔数对上后要多读一段再解析（见 `_JPEG_DEEP_BYTES`）。
"""
from __future__ import annotations

import struct
from pathlib import Path

# 蚁小二抖音图文 schema 接受的格式（实测自 3.2.28 报错信息）
ALLOWED_IMAGE_FORMATS = ("jpg", "jpeg", "png", "webp")


def _png(b: bytes):
    if len(b) >= 24 and b[12:16] == b"IHDR":
        w, h = struct.unpack(">II", b[16:24])
        return "png", w, h
    return None


def _gif(b: bytes):
    if len(b) >= 10 and b[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", b[6:10])
        return "gif", w, h
    return None


def _jpeg(b: bytes):
    if b[:3] != b"\xff\xd8\xff":
        return None
    i, n = 2, len(b)
    while i + 9 < n:
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:   # SOI/EOI/RSTn
            i += 2
            continue
        if marker == 0xDA:                                     # SOS：后面是扫描数据
            return None
        seg = struct.unpack(">H", b[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):      # SOF 系列
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return "jpg", w, h
        i += 2 + seg
    return None


def _webp(b: bytes):
    if len(b) < 30 or b[:4] != b"RIFF" or b[8:12] != b"WEBP":
        return None
    tag = b[12:16]
    if tag == b"VP8X":
        w = int.from_bytes(b[24:27], "little") + 1
        h = int.from_bytes(b[27:30], "little") + 1
        return "webp", w, h
    if tag == b"VP8 ":
        w = int.from_bytes(b[26:28], "little") & 0x3FFF
        h = int.from_bytes(b[28:30], "little") & 0x3FFF
        return "webp", w, h
    if tag == b"VP8L":
        bits = int.from_bytes(b[21:25], "little")
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return "webp", w, h
    return None


# JPEG 的 SOF 可能深达 100KB+：实测方舟 Seedream 的 1536x2720 图，SOF 在
# 第 99,515~126,433 字节（前面是大段 EXIF/XMP）。只读 64 字节会把好图判成
# "无法识别" → 发布直接失败（实测 6 张真图全被误杀，连撞 11 条失败）。
_JPEG_DEEP_BYTES = 1024 * 1024


def _hit(got, size: int) -> dict | None:
    if not got:
        return None
    fmt, w, h = got
    return {"format": fmt, "width": int(w), "height": int(h), "size": int(size)}


def probe(path) -> dict | None:
    """返回 `{"format","width","height","size"}`；无法识别或读取失败返回 None。"""
    p = Path(path)
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            head = fh.read(64)
    except OSError:
        return None

    got = _hit(_png(head) or _gif(head) or _webp(head), size)
    if got:
        return got

    # JPEG：魔数对上后**多读一段**再找 SOF（见 _JPEG_DEEP_BYTES 的说明）
    if head[:3] == b"\xff\xd8\xff":
        try:
            with p.open("rb") as fh:
                deep = fh.read(min(size, _JPEG_DEEP_BYTES))
        except OSError:
            return None
        return _hit(_jpeg(deep), size)
    return None


__all__ = ["ALLOWED_IMAGE_FORMATS", "probe"]
