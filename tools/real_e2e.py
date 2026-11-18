"""真实 LLM 全链路验证（T→A→B→C）：跑通 + 输出兼容性诊断。

用法（项目根目录）：
    ./.venv/Scripts/python.exe tools/real_e2e.py                 # 默认标题跑 A→B→C
    ./.venv/Scripts/python.exe tools/real_e2e.py --titles 3      # 加跑 T 段选题

前提：系统设置页已配真实模型（`data/model_config.json` 的 `llm` 段，
provider ≠ fake）。脚本会先做装配探针，fake 模式直接退出。

诊断维度（真模型输出的常见病）：
  T 段：编号前缀（"1. "）是否污染入库、空行/重复
  A 段：Markdown 围栏、首尾散文、无正文标签、空输出
  B 段：直解 JSON 是否成功、是否触发抢救路径、location 丢弃几条
  C 段：围栏、首尾散文污染

退出码：0 = 链路跑通且无新增兼容性问题；1 = 链路断或发现问题。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop.paths import pin_environment                     # noqa: E402

pin_environment()                                             # 必须在 app.* import 之前

from app import create_app                                   # noqa: E402
from app.services.llm import build_llm_client                # noqa: E402
from app.services.llm.providers import OpenAICompatProvider  # noqa: E402
from app.services.model_config import effective_llm          # noqa: E402
from app.services.writer.pipeline import generate_article, generate_titles  # noqa: E402
from app.services.writer.review import review_article, revise_article  # noqa: E402
from app.tenancy import tenant_scope                         # noqa: E402

_STEP_MARKS = (
    ("T", "行业中性"),
    ("A", "【本篇写作视角】"),
    ("B", "【待审文章】"),
    ("C", "【审稿报告】"),
)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_TAG = re.compile(r"<(p|h2|h3|table|ul|ol|li|blockquote)[\s>]", re.I)
_NUM_PREFIX = re.compile(r"^\s*(?:\d+\s*[.、．]|[(（]\d+[)）]|[一二三四五六七八九十]+\s*[.、．])\s*")


def classify(prompt: str) -> str:
    for step, mark in _STEP_MARKS:
        if mark in prompt:
            return step
    return "?"


def html_diagnose(label: str, raw: str, problems: list[str]) -> None:
    """A/C 段 HTML 输出的兼容性体检。"""
    text = (raw or "").strip()
    if not text:
        problems.append(f"{label}: 空输出")
        return
    if text.startswith("```"):
        problems.append(f"{label}: 输出被 Markdown 围栏包裹")
    head = text[: text.find("<")] if "<" in text else text
    if head.strip():
        problems.append(f"{label}: 首个标签前有散文 {head.strip()[:24]!r}")
    tail = text[text.rfind(">") + 1:] if ">" in text else ""
    if tail.strip():
        problems.append(f"{label}: 最后一个标签后有散文 {tail.strip()[:24]!r}")
    if not _TAG.search(text):
        problems.append(f"{label}: 未见正文 HTML 标签（模型可能输出了纯文本/Markdown）")


def review_diagnose(raw: str, draft: str, problems: list[str]) -> dict:
    """B 段 JSON 的兼容性体检：直解 / 围栏 / 尾逗号 / 值内裸引号 / location 丢弃。"""
    info: dict = {}
    text = (raw or "").strip()
    info["fenced"] = text.startswith("```")
    try:
        data = json.loads(_FENCE.sub("", text), strict=False)
        info["direct_json"] = True
    except json.JSONDecodeError:
        data = None
        info["direct_json"] = False
        problems.append("B: 直解 JSON 失败（依赖抢救路径）")
    if isinstance(data, dict):
        n_raw = len(data.get("issues") or [])
        info["issues_raw"] = n_raw
        # 复算 location 丢弃：走正式解析器对比
        from app.services.writer.review_compat import parse_review

        parsed = parse_review(raw, article=draft)
        dropped = n_raw - len(parsed.get("issues") or [])
        info["issues_kept"] = len(parsed.get("issues") or [])
        if dropped:
            problems.append(f"B: {dropped} 条 issue 因 location 定位不到原文被丢弃")
        verdict = str(data.get("verdict") or "")
        if verdict not in ("可直接发布", "需要修改", "建议重写"):
            problems.append(f"B: verdict 非冻结枚举 {verdict!r}（依赖别名归一）")
    return info


def title_diagnose(raw: str, problems: list[str]) -> None:
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    numbered = [ln for ln in lines if _NUM_PREFIX.match(ln)]
    if numbered:
        problems.append(f"T: {len(numbered)}/{len(lines)} 行带编号前缀"
                        f"（入库会残留 '1. ' 等字符）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="杭州金缮修复大概什么价位对比三家工坊的报价差在哪")
    ap.add_argument("--titles", type=int, default=0, help="额外跑 T 段生成 N 个标题")
    args = ap.parse_args()

    app = create_app("dev")
    problems: list[str] = []
    captured: list[tuple[str, str, str]] = []      # (step, prompt, raw)

    # ── 探针：确认真实模型已配置 ────────────────────────────────────────────
    with app.app_context():
        from app.config import get_config

        eff = effective_llm(get_config())
    provider = (eff["provider"] or "fake").lower()
    if provider == "fake":
        print("[ABORT] 当前是 fake 模式：请先在系统设置页配置真实 LLM（data/model_config.json）")
        return 1
    print(f"[PROBE] provider={provider} model={eff['model']} base_url={eff['base_url'] or '(预设)'}"
          f" keys={len(eff['keys'])}")

    # ── 埋点：捕获真实 provider 的每次原始返回 ──────────────────────────────
    _orig_complete = OpenAICompatProvider.complete

    def _spy(self, messages, **kw):
        out = _orig_complete(self, messages, **kw)
        prompt = next((m.get("content", "") for m in reversed(messages)
                       if m.get("role") == "user"), "")
        captured.append((classify(prompt), prompt, out))
        return out

    OpenAICompatProvider.complete = _spy

    exit_code = 0
    try:
        with app.app_context(), tenant_scope("local"):
            # T 段（可选）
            if args.titles:
                titles = generate_titles(args.titles)
                t_raw = next((r for s, _p, r in captured if s == "T"), "")
                title_diagnose(t_raw, problems)
                print(f"[T] 生成 {len(titles)} 个标题：")
                for t in titles:
                    print(f"    - {t}")

            # A → B → C
            print(f"[A] 生成初稿：{args.title}")
            a = generate_article(title_text=args.title)
            print(f"    status={a['status']} chars={a.get('chars')}")
            if a["status"] != "drafted":
                problems.append(f"A: 未产出初稿（{a}）")

            print("[B] 审稿…")
            b = review_article(a["dir"])
            print(f"    status={b['status']} verdict={b.get('verdict')}"
                  f" score={b.get('score')} issues={b.get('issues')}")

            print("[C] 修订…")
            c = revise_article(a["dir"])
            print(f"    status={c['status']} source={c.get('source')}")

            stages = {"A": a, "B": b, "C": c}
            for name, out in stages.items():
                if out.get("status") in ("skipped",) and name == "A":
                    problems.append("A: 目录已存在导致跳过（换个标题）")

            # ── 诊断（留在租户上下文内：storage 按租户分目录）───────────────
            from app.platform import storage
            from app.services.writer.pipeline import slugify

            draft_text = ""
            if storage.checkpoint(a["dir"])[storage.DRAFT]:
                draft_text = storage.read_article_file(a["dir"], storage.DRAFT)
            for step, _prompt, raw in captured:
                if step in ("A", "C"):
                    html_diagnose(step, raw, problems)
                elif step == "B":
                    review_diagnose(raw, draft_text, problems)
    except Exception as exc:                       # 链路断：如实呈现
        print(f"[FAIL] 链路异常：{type(exc).__name__}: {exc}")
        problems.append(f"链路异常：{type(exc).__name__}: {exc}")
        exit_code = 1
    finally:
        OpenAICompatProvider.complete = _orig_complete

    print("\n===== 诊断报告 =====")
    if problems:
        for p in problems:
            print(f"  [问题] {p}")
        exit_code = 1
    else:
        print("  未发现兼容性问题 ✅")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
