"""体检一篇已生成文章的定稿：结构要素 + 合规红线 + 审稿分。

用法：.venv\\Scripts\\python.exe tools\\inspect_article.py [文章目录名]
不给目录名则取最新一篇。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ART = ROOT / "data" / "local" / "articles"


def main() -> int:
    if len(sys.argv) > 1:
        d = ART / sys.argv[1]
    else:
        d = sorted([p for p in ART.iterdir() if p.is_dir()],
                   key=lambda p: p.stat().st_mtime, reverse=True)[0]
    print(f"目录: {d.name}\n")

    for name in ("01_draft.html", "03_final.html"):
        p = d / name
        if not p.exists():
            print(f"  {name}: 缺")
            continue
        h = p.read_text(encoding="utf-8")
        t = re.sub(r"\s", "", re.sub(r"<[^>]+>", "", h))
        print(f"  {name}: {len(h.encode('utf-8'))} 字节 / 正文 {len(t)} 字")

    final = (d / "03_final.html")
    if final.exists():
        h = final.read_text(encoding="utf-8")
        t = re.sub(r"\s", "", re.sub(r"<[^>]+>", "", h))
        print("\n=== 规格七类要素 ===")
        checks = {
            "定义句 XX，是指": bool(re.search(r"[，,]\s*是指", t)),
            "结论句加粗": bool(re.search(r"<strong>[^<]{10,}</strong>", h)),
            "表格（≥3 行）": len(re.findall(r"<tr", h)) >= 3,
            "编号列表（≥3 步）": len(re.findall(r"<li", h)) >= 3,
            "适用边界（不建议/不适合）": any(k in t for k in ("不建议", "不适合", "不适合做")),
            "Q&A": ("Q：" in t or "Q:" in t or "问：" in t),
            "时效信号": bool(re.search(r"20\d{2}\s*年", t)),
        }
        for k, v in checks.items():
            print(f"  {'有' if v else '缺'}  {k}")

        print("\n=== 合规红线 ===")
        phones = re.findall(r"1[3-9]\d{9}|\b0\d{2,3}-?\d{7,8}\b", h)
        bad = [w for w in ("最好", "最佳", "独家", "一口价", "非遗", "老字号", "全国连锁",
                           "永久不坏", "零遗失", "百分百", "绝对", "唯一", "顶级",
                           "国家级", "彻底解决", "保证", "根治", "完美修复") if w in t]
        empty = len(re.findall(r"<(strong|b|span)\b[^>]*>\s*</\1>", h))
        dangling = re.findall(r"(?:现在就拨|拨打|致电)\s*[。，,]", t)
        print(f"  号码: {phones or '无 ✓'}")
        print(f"  绝对化/违禁: {bad or '无 ✓'}")
        print(f"  空标签: {empty}{' ✓' if empty == 0 else ' ⚠'}")
        print(f"  悬空动词: {dangling or '无 ✓'}")

    for name, label in (("02_review.json", "初稿审稿"), ("04_final_review.json", "定稿复评")):
        p = d / name
        if p.exists():
            r = json.loads(p.read_text(encoding="utf-8"))
            ds = r.get("dimension_scores") or {}
            short = {k.split("：")[-1]: v for k, v in ds.items()}
            print(f"\n{label}: {r.get('overall_score')} 分 / {r.get('verdict')} / "
                  f"{len(r.get('issues') or [])} 个问题")
            if short:
                print("  " + "  ".join(f"{k}={v}" for k, v in short.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
