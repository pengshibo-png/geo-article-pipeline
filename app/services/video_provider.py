"""AI 短视频产线的成片渲染 provider（可插拔：mock 演示 / real 供应商）。

- **mock（默认，离线）**：ffmpeg 合成确定性演示片 —— 竖版 9:16（540×960）
  深蓝底 + 烧字（标题卡 + 分镜卡片依次显示），约 8-12 秒。不联网、无外部
  Key，页面全流程可演；元数据里 `source=mock` 如实标注，不假装真实成片。
- **real**：OpenAI 兼容视频端点（base_url+model+key 配在系统设置 `video` 段）
  异步提交 → 轮询 → 下载 mp4。未配置时如实报错，绝不回落成 mock 冒充真实。

ffmpeg 解析顺序：`imageio_ffmpeg` 随包二进制 → PATH。两者都没有 →
PublishError 级"引擎缺失"（调用方转用户可读文案）。

文字烧录的字体经相对路径传入（filter 串里驱动盘冒号转义不可靠，实测）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from app.errors import UpstreamError
from app.logging import get_logger
from app.platform.fsutil import remove_tree
from app.services.model_config import load_overrides

log = get_logger(__name__)

_W, _H = 540, 960                     # 竖版 9:16
_BG = "0x1F4E79"                      # 深蓝（与封面兜底同色，不绑定品牌）
_ACCENT = "0xB08D57"                  # 铜金（ds.css token 的 hex）
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def ffmpeg_exe() -> str | None:
    """ffmpeg 可执行文件：随包二进制优先，PATH 兜底。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                 # noqa: BLE001 — 包缺失/平台不支持 → PATH
        return shutil.which("ffmpeg")


def _find_font() -> Path | None:
    for f in _FONT_CANDIDATES:
        p = Path(f)
        if p.exists():
            return p
    return None


def _esc(text: str) -> str:
    """drawtext 文本转义：filter 分隔符与引号。"""
    return (str(text or "")[:40]
            .replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\\'").replace(",", "\\,")
            .replace("[", "\\[").replace("]", "\\]"))


def render_mock(*, title: str, shots: list[str], duration: int = 10) -> bytes:
    """确定性演示片：标题卡 + 分镜卡片（ffmpeg lavfi + drawtext）。

    shots 每条显示 duration/(len+1) 秒（enable 表达式按时间段开关）。
    无可用字体时退化为纯色片（元数据仍如实标注 mock）。
    """
    exe = ffmpeg_exe()
    if not exe:
        raise UpstreamError("视频引擎缺失：请安装 imageio-ffmpeg（随包）或系统 ffmpeg")
    work = Path(tempfile.mkdtemp(prefix="geo-video-"))
    try:
        font = _find_font()
        filters = []
        cards = [title] + [s for s in shots if str(s).strip()][:6]
        seg = max(1.0, duration / max(1, len(cards)))
        for i, card in enumerate(cards):
            if font is None:
                continue
            color = "white" if i == 0 else _ACCENT
            size = 46 if i == 0 else 34
            y = 380 if i == 0 else 420
            enable = f"between(t,{i * seg:.1f},{(i + 1) * seg:.1f})"
            filters.append(
                f"drawtext=fontfile=font.ttf:text='{_esc(card)}':fontcolor={color}:"
                f"fontsize={size}:x=(w-text_w)/2:y={y}:enable='{enable}'")
        if font is not None:
            shutil.copy(font, work / "font.ttf")
        vf = ",".join(filters) if filters else "null"
        out = work / "video.mp4"
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", f"color=c={_BG}:s={_W}x{_H}:d={duration}",
               "-vf", vf, "-pix_fmt", "yuv420p", "-movflags", "+faststart",
               "video.mp4"]
        proc = subprocess.run(cmd, capture_output=True, timeout=180, cwd=str(work),
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not out.exists():
            raise UpstreamError(f"演示片渲染失败：{(proc.stderr or '')[:200]}")
        return out.read_bytes()
    finally:
        remove_tree(work, ignore_errors=True)


def render_cover(*, title: str, shots: list[str] | None = None) -> bytes:
    """封面帧：从演示片第 0.5 秒截帧（无 ffmpeg 时返回纯色 PNG 兜底）。"""
    exe = ffmpeg_exe()
    if not exe:
        return _solid_png()
    work = Path(tempfile.mkdtemp(prefix="geo-cover-"))
    try:
        font = _find_font()
        filters = []
        if font is not None:
            shutil.copy(font, work / "font.ttf")
            filters.append(
                f"drawtext=fontfile=font.ttf:text='{_esc(title)[:20]}':fontcolor=white:"
                f"fontsize=40:x=(w-text_w)/2:y=420")
        vf = ",".join(filters) if filters else "null"
        out = work / "cover.png"
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", f"color=c={_BG}:s={_W}x{_H}:d=1",
               "-vf", vf, "-frames:v", "1", "cover.png"]
        proc = subprocess.run(cmd, capture_output=True, timeout=60, cwd=str(work),
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not out.exists():
            return _solid_png()
        return out.read_bytes()
    finally:
        remove_tree(work, ignore_errors=True)


def _solid_png() -> bytes:
    """无 ffmpeg 时的纯色封面（与 yxer_cover 同款手写 PNG，零依赖）。"""
    import struct
    import zlib

    w, h = _W, _H
    rgb = (31, 78, 121)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = (b"\x00" + bytes(rgb) * w) * h
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# ── real：OpenAI 兼容视频端点（系统设置 video 段） ──────────────────────────

def video_conf() -> dict:
    ov = load_overrides().get("video")
    return dict(ov) if isinstance(ov, dict) else {}


def generate_real(*, prompt: str, duration: int = 10) -> bytes:
    """真实视频生成：提交 → 轮询 → 下载。未配置/失败一律如实抛错。"""
    conf = video_conf()
    base = str(conf.get("base_url") or "").strip()
    key = str(conf.get("key") or "").strip()
    model = str(conf.get("model") or "").strip()
    if not (base and key and model):
        raise UpstreamError("未配置视频生成供应商：请在系统设置填写 video 段"
                            "（base_url/model/key），或使用演示模式")
    body = json.dumps({"model": model, "prompt": prompt,
                       "duration": duration}).encode()
    from app.platform import net      # 忽略系统代理（VPN 代理会断 TLS，见 platform/net.py）

    req = urllib.request.Request(
        base.rstrip("/") + "/video/generations", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with net.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                      # noqa: BLE001 — 如实转译
        raise UpstreamError(f"视频生成提交失败：{str(exc)[:160]}") from exc
    url = ((data.get("data") or [{}])[0].get("url")
           or data.get("url") or "")
    if not url:
        raise UpstreamError(f"视频生成响应无可下载地址：{str(data)[:160]}")
    deadline = time.monotonic() + 600             # 单条 6-10 分钟的量级
    while time.monotonic() < deadline:
        try:
            with net.urlopen(url, timeout=120) as resp:
                if resp.status == 200:
                    return resp.read()
        except Exception:                         # noqa: BLE001 — 未就绪继续轮询
            pass
        time.sleep(10)
    raise UpstreamError("视频生成超时（10 分钟）：请稍后在供应商侧查看任务")
