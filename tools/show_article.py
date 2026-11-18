"""打印一篇定稿的正文摘要（给人工快速过目用）。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ART = ROOT / "data" / "local" / "articles"


def main() -> int:
    d = ART / sys.argv[1] if len(sys.argv) > 1 else sorted(
        [p for p in ART.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime, reverse=True)[0]
    h = (d / "03_final.html").read_text(encoding="utf-8")
    # 保留结构信号：h2 加【】、表格行用 | 分隔
    h = re.sub(r"<h2[^>]*>\s*(.*?)\s*</h2>", r"\n\n## \1\n", h, flags=re.S)
    h = re.sub(r"<tr[^>]*>", "\n| ", h)
    h = re.sub(r"</t[dh]>\s*<t[dh][^>]*>", " | ", h)
    h = re.sub(r"<li[^>]*>", "\n- ", h)
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r"</(p|tr|table|ul|ol|blockquote)>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = re.sub(r"\n{3,}", "\n\n", h).strip()
    print(h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
