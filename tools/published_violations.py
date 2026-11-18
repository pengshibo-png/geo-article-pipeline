import re, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.services.writer import compliance

ART = ROOT / "data" / "local" / "articles"
con = sqlite3.connect(ROOT / "data" / "app.db")
con.row_factory = sqlite3.Row
rows = con.execute("SELECT id, dir, title, status FROM article WHERE status='published'").fetchall()
con.close()
BAD = ["非遗", "老字号", "全国连锁", "双直营", "独家", "一口价", "零遗失风险",
       "永久不坏", "百分百无痕", "无损修复", "最好", "最佳", "第一", "唯一",
       "国家级", "绝对", "100%", "微信", "扫码", "私信", "立即预约", "拨打"]
for r in rows:
    d = ART / r["dir"]
    p = d / "03_final.html"
    if not p.exists():
        p = d / "01_draft.html"
    if not p.exists():
        continue
    text = re.sub(r"<[^>]+>", "", p.read_text(encoding="utf-8"))
    text = re.sub(r"\s+", " ", text)
    hits = []
    for w in BAD:
        i = text.find(w)
        if i >= 0:
            hits.append((w, text[max(0, i - 18):i + 22].strip()))
    print("=== #%s %s ===" % (r["id"], r["title"][:34]))
    if not hits:
        print("   （正文未检出违禁表述）")
    for w, ctx in hits:
        print("   [%s] …%s…" % (w, ctx))
    print()