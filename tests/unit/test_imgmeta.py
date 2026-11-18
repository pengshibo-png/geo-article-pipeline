"""图片探针回归测试（2026-10-19 事故）。

事故：切到方舟 Seedream 后，生图返回的是 **JPEG**，而 `probe()` 只读文件头
64 字节去找 SOF 段。实测这些图的 SOF 在第 **99,515~126,433** 字节（前面挂着
大段 EXIF/XMP），于是 6 张合法 JPEG 全被判成"无法识别" → 抖音图文发布直接失败
（账本里连撞 11 条 `配图格式抖音不支持`）。旧包之所以能发，是因为那时出的是 PNG
（尺寸信息在第 16 字节内）。
"""
from __future__ import annotations

import struct

from app.services.publish.imgmeta import probe


def _jpeg_with_pad(pad: int, w: int, h: int) -> bytes:
    """造一个 SOF 被推到文件深处（pad 字节之后）的合法 JPEG 头。

    段长字段是 16 位（≤65535），所以大的前置数据要拆成多个 APP0 段
    —— 真实 JPEG 也是这么排的（EXIF / XMP / ICC 各占一段）。
    """
    out = b"\xff\xd8"                       # SOI
    left = pad
    while left > 0:
        chunk = min(left, 60_000)
        out += b"\xff\xe0" + struct.pack(">H", 2 + chunk) + b"\x00" * chunk
        left -= chunk
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
           + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9)
    return out + sof + b"\xff\xd9"           # SOF0 + EOI


def test_probe_jpeg_when_sof_is_deep_in_file(tmp_path):
    """SOF 在 110KB 之后（实测方舟图的真实形态）也必须识别出宽高。"""
    p = tmp_path / "deep.jpg"
    p.write_bytes(_jpeg_with_pad(110_000, 1536, 2720))

    info = probe(p)
    assert info is not None, "合法 JPEG 不能被判成无法识别"
    assert info["format"] == "jpg"
    assert (info["width"], info["height"]) == (1536, 2720)
    assert info["size"] == p.stat().st_size


def test_probe_small_jpeg_still_works(tmp_path):
    """短的 JPEG（SOF 在前 64 字节内）走快路径，也不能坏。"""
    p = tmp_path / "small.jpg"
    p.write_bytes(_jpeg_with_pad(4, 800, 600))

    info = probe(p)
    assert info and info["format"] == "jpg"
    assert (info["width"], info["height"]) == (800, 600)


def test_probe_png_still_works(tmp_path):
    """PNG 走 64 字节快路径（尺寸在 IHDR 里）。"""
    ihdr = b"IHDR" + struct.pack(">II", 1024, 768) + b"\x08\x02\x00\x00\x00"
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + b"\x00\x00\x00\x00"
    p = tmp_path / "a.png"
    p.write_bytes(png)

    info = probe(p)
    assert info and info["format"] == "png"
    assert (info["width"], info["height"]) == (1024, 768)


def test_probe_rejects_non_image(tmp_path):
    """SVG 占位图等非位图仍然要被拒（这是原本就想拦的东西）。"""
    p = tmp_path / "a.svg"
    p.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    assert probe(p) is None


def test_probe_missing_file_returns_none(tmp_path):
    assert probe(tmp_path / "nope.jpg") is None
