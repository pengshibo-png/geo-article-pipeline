"""端到端验证文章流水线：触发一篇 → 轮询 → 检查产物与合规清洗。

用法：
    .venv\\Scripts\\python.exe tools\\verify_pipeline.py [--count 1] [--timeout 600]

检查项（对应 2026-10-22 的三处修复）：
1. 不再因 `finish_reason=length` 降级（日志里不应出现 UpstreamError/降级）；
2. 二审能产出 02_review.json（B 段跑通）；
3. 03_final.html 与 01_draft.html 不是同一份（说明 C 段真的改了，不是 draft-fallback）；
4. 日志里若有 article_compliance_sanitized，打印命中的违规项（号码/绝对化用语）。
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
import http.cookiejar
from pathlib import Path

BASE = "http://127.0.0.1:7001"
ROOT = Path(__file__).resolve().parent.parent
ART = ROOT / "data" / "local" / "articles"

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_tok = {"v": ""}


def call(method, path, payload=None, timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    if method != "GET":
        if not _tok["v"]:
            with _opener.open(BASE + "/api/csrf", timeout=10) as r:
                _tok["v"] = json.loads(r.read().decode()).get("token", "")
        h["X-CSRFToken"] = _tok["v"]
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    st, b = call("POST", "/api/run", {"count": args.count, "no_publish": True})
    print(f"[run] {st} {b[:200]}")
    if st != 200 or not json.loads(b).get("ok"):
        return 1

    deadline = time.time() + args.timeout
    seen: list[str] = []
    while time.time() < deadline:
        time.sleep(10)
        st, b = call("GET", "/api/status")
        d = json.loads(b)
        pr = d.get("progress") or {}
        for line in (d.get("log_tail") or []):
            if line not in seen:
                seen.append(line)
                print("   ", str(line)[:260])
        if not d.get("running"):
            print(f"[done] success={pr.get('success')} fail={pr.get('fail')} total={pr.get('total')}")
            break
    else:
        print("[timeout] 仍在运行")

    print("\n=== 产物检查（最近 1 个目录）===")
    dirs = sorted([p for p in ART.iterdir() if p.is_dir()],
                  key=lambda p: p.stat().st_mtime, reverse=True)[:1]
    for d in dirs:
        files = {p.name: p for p in d.iterdir()}
        print(f"  {d.name}")
        print(f"    01_draft : {'有' if '01_draft.html' in files else '缺'} "
              f"{files['01_draft.html'].stat().st_size if '01_draft.html' in files else ''}")
        print(f"    02_review: {'有' if '02_review.json' in files else '缺'}")
        print(f"    03_final : {'有' if '03_final.html' in files else '缺'} "
              f"{files['03_final.html'].stat().st_size if '03_final.html' in files else ''}")
        if "01_draft.html" in files and "03_final.html" in files:
            same = files["01_draft.html"].read_bytes() == files["03_final.html"].read_bytes()
            print(f"    03 == 01（降级回退的标志）: {'是 ⚠' if same else '否 ✓'}")
        if "02_review.json" in files:
            rv = json.loads(files["02_review.json"].read_text(encoding="utf-8"))
            print(f"    审稿: score={rv.get('overall_score')} verdict={rv.get('verdict')} "
                  f"issues={len(rv.get('issues') or [])}")
        if "03_final.html" in files:
            h = files["03_final.html"].read_text(encoding="utf-8")
            text = re.sub(r"<[^>]+>", "", h)
            phones = re.findall(r"1[3-9]\d{9}|\b0\d{2,3}-?\d{7,8}\b", h)
            bad = [w for w in ("最好", "最佳", "第一", "唯一", "顶级", "国家级",
                               "100%", "绝对", "永不", "终身", "零风险", "保证", "根治")
                   if w in text]
            print(f"    号码: {phones if phones else '无 ✓'}")
            print(f"    绝对化用语: {bad if bad else '无 ✓'}")
            print(f"    正文字数: {len(re.sub(r'\\s', '', text))}")
    hits = [l for l in seen if "compliance" in l or "截断" in l or "降级" in l]
    print(f"\n=== 合规/预算相关日志 {len(hits)} 条 ===")
    for l in hits:
        print("   ", l[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
