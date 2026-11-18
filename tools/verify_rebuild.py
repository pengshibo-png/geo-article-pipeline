import json, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.services.writer import compliance

ART = ROOT / "data" / "local" / "articles"
PLAN = ROOT / "data" / "rebuild-prepare.json"
MIN = 2000
con = sqlite3.connect(ROOT / "data" / "app.db")
con.row_factory = sqlite3.Row
rows = con.execute("SELECT id, dir, title FROM article ORDER BY id").fetchall()
con.close()
targets = set()
if PLAN.exists():
    targets = {x["title"] for x in json.loads(PLAN.read_text(encoding="utf-8"))["moved"]}
ok = bad = 0
for r in rows:
    if r["title"] not in targets:
        continue
    d = ART / r["dir"]
    p = d / "03_final.html"
    if not p.exists():
        p = d / "01_draft.html"
    if not p.exists():
        print("       %3s  -- 尚未产出" % r["id"])
        continue
    html = p.read_text(encoding="utf-8")
    chars = compliance.visible_chars(html)
    miss = compliance.missing_elements(html)
    _c, hits = compliance.sanitize(html, contact_mode="revise")
    real = [h for h in hits if not h.startswith(("空", "悬空", "时效"))]
    good = (not miss) and chars >= MIN and not real
    ok += good
    bad += (not good)
    rv = d / "04_final_review.json"
    score = "-"
    if rv.exists():
        try:
            score = json.loads(rv.read_text(encoding="utf-8")).get("overall_score")
        except Exception:
            pass
    det = []
    if miss:
        det.append("缺" + "/".join(miss))
    if real:
        det.append("违规:" + ",".join(sorted({h.split(":")[0] for h in real})))
    if chars < MIN:
        det.append("字数%d" % chars)
    tag = "PASS" if good else "WARN"
    print("  %s %3s %5d %4s  %-24s %s" % (tag, r["id"], chars, score, r["title"][:22], "；".join(det) or "全部达标"))
print("\n达标 %d / 未达标 %d" % (ok, bad))