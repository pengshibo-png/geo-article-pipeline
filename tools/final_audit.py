import re, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.services.writer import compliance

ART = ROOT / "data" / "local" / "articles"
con = sqlite3.connect(ROOT / "data" / "app.db")
con.row_factory = sqlite3.Row
rows = con.execute("SELECT id, dir, title FROM article ORDER BY id").fetchall()
con.close()
ok = bad = 0
badlist = []
for r in rows:
    d = ART / r["dir"]
    p = d / "03_final.html"
    if not p.exists():
        p = d / "01_draft.html"
    if not p.exists():
        continue
    html = p.read_text(encoding="utf-8")
    chars = compliance.visible_chars(html)
    miss = compliance.missing_elements(html)
    # 违规判定用 sanitize 的真实命中（已含否定语境保护），不用裸 in 检查
    _c, hits = compliance.sanitize(html, contact_mode="revise")
    real = [h for h in hits if not h.startswith(("空", "悬空", "时效"))]
    good = (not miss) and chars >= 2000 and not real
    ok += good
    bad += (not good)
    if not good:
        det = []
        if miss:
            det.append("缺" + "/".join(miss))
        if real:
            det.append("违规:" + ",".join(sorted({h.split(":")[0] for h in real})))
        if chars < 2000:
            det.append("字数%d" % chars)
        badlist.append("  #%s %d字 %s %s" % (r["id"], chars, r["title"][:26], "；".join(det)))
print("文章档案 %d 篇：平台合规且要素齐全 %d 篇，待完善 %d 篇" % (len(rows), ok, bad))
print("（违规判定已用带否定语境保护的 sanitize，不再把「不按一口价」误报成违规）")
for l in badlist:
    print(l)