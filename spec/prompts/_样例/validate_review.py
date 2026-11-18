"""B 段审稿输出的契约校验器（S3 冻结契约的可执行版本）

用途：
  1. 验收样例输出是否合法（本文件可直接运行）
  2. 作为 S4 实现方写 B 段响应解析器的规格依据

用法：python validate_review.py [_样例/B-审稿输出-样例.json]
"""
import json
import sys

# ---- 冻结契约（与 prompts/B-审稿质检.md 的【输出格式】逐条一致）----
VERDICTS = {"可直接发布", "需要修改", "建议重写"}
SEVERITIES = {"严重", "建议", "微调"}
DIMENSIONS = {
    "维度一：事实与专业准确性",
    "维度二：逻辑严密性",
    "维度三：结构完整性与GEO可引用性",
    "维度四：专业深度与信息密度",
    "维度五：品牌融入质量",
    "维度六：文字质量与阅读体验",
    "维度七：合规与风险",
}
TOP_KEYS = {"overall_score", "verdict", "summary", "issues", "highlights",
            "dimension_scores"}
# 后加字段：**可选**。给了就必须七维齐全且合法；不给则由适配层加权逻辑兜底
OPTIONAL_KEYS = {"dimension_scores"}
ISSUE_KEYS = {"severity", "dimension", "location", "problem", "suggestion"}


def validate(d: dict, article_text: str | None = None) -> list[str]:
    """返回错误列表；空列表 = 通过"""
    e: list[str] = []

    # 1) 顶层字段
    missing = (TOP_KEYS - OPTIONAL_KEYS) - set(d)
    if missing:
        e.append(f"缺少顶层字段: {sorted(missing)}")
    extra = set(d) - TOP_KEYS
    if extra:
        e.append(f"出现契约外的顶层字段: {sorted(extra)}")

    # 2) overall_score
    s = d.get("overall_score")
    if not isinstance(s, int) or isinstance(s, bool):
        e.append(f"overall_score 必须是整数，实际: {type(s).__name__}")
    elif not (0 <= s <= 100):
        e.append(f"overall_score 越界: {s}（应为 0-100）")

    # 3) verdict
    v = d.get("verdict")
    if v not in VERDICTS:
        e.append(f"verdict 非法: {v!r}（应为 {sorted(VERDICTS)}）")

    # 4) summary
    if not isinstance(d.get("summary"), str) or not d.get("summary", "").strip():
        e.append("summary 必须是非空字符串")

    # 5) highlights
    h = d.get("highlights")
    if not isinstance(h, list) or not all(isinstance(x, str) for x in h):
        e.append("highlights 必须是字符串数组")
    elif not (0 <= len(h) <= 3):
        e.append(f"highlights 数量越界: {len(h)}（应为 0-3）")

    # 6) issues
    issues = d.get("issues")
    if not isinstance(issues, list):
        e.append("issues 必须是数组")
    else:
        for i, it in enumerate(issues):
            p = f"issues[{i}]"
            if not isinstance(it, dict):
                e.append(f"{p} 必须是对象"); continue
            mi = ISSUE_KEYS - set(it)
            if mi:
                e.append(f"{p} 缺字段: {sorted(mi)}")
            if it.get("severity") not in SEVERITIES:
                e.append(f"{p}.severity 非法: {it.get('severity')!r}")
            if it.get("dimension") not in DIMENSIONS:
                e.append(f"{p}.dimension 非法: {it.get('dimension')!r}")
            for k in ("location", "problem", "suggestion"):
                if not isinstance(it.get(k), str) or not it.get(k, "").strip():
                    e.append(f"{p}.{k} 必须是非空字符串")
            # location 必须能在原文找到（若提供了原文）
            if article_text and isinstance(it.get("location"), str):
                frag = it["location"].strip().strip('"“”')
                if len(frag) >= 8 and frag not in article_text:
                    e.append(f"{p}.location 未在原文中找到（要求引用真实片段）: {frag[:30]}…")

    # 7) dimension_scores（可选；给了就必须七维齐全且为 0-100 整数）
    ds = d.get("dimension_scores")
    if ds is not None:
        if not isinstance(ds, dict):
            e.append("dimension_scores 必须是对象")
        else:
            miss_dim = DIMENSIONS - set(ds)
            if miss_dim:
                e.append(f"dimension_scores 缺维度: {sorted(miss_dim)}")
            for k, val in ds.items():
                if k not in DIMENSIONS:
                    e.append(f"dimension_scores 维度非法: {k!r}")
                elif isinstance(val, bool) or not isinstance(val, int) \
                        or not (0 <= val <= 100):
                    e.append(f"dimension_scores[{k}] 必须是 0-100 的整数")

    # 8) verdict ↔ score 映射（冻结规则）
    if isinstance(s, int) and v in VERDICTS:
        if s >= 90 and v != "可直接发布":
            e.append(f"映射冲突: score={s} 应为 可直接发布，实际 {v}")
        if 60 <= s <= 89 and v != "需要修改":
            e.append(f"映射冲突: score={s} 应为 需要修改，实际 {v}")
        if s < 60 and v != "建议重写":
            e.append(f"映射冲突: score={s} 应为 建议重写，实际 {v}")

    # 9) 一票否决已取消（2026-10-17 决策）：严重问题只按维度扣分，不再直接压总分。
    #    这里刻意留一行说明，避免后来者以为漏了校验。

    return e


if __name__ == "__main__":
    fp = sys.argv[1] if len(sys.argv) > 1 else "_样例/B-审稿输出-样例.json"
    raw = open(fp, encoding="utf-8").read()

    # 门禁 0：必须是纯 JSON（不能被 ``` 包裹）
    if raw.lstrip().startswith("```"):
        print("❌ 输出被 Markdown 代码块包裹——下游 json.loads 会失败")
        sys.exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as ex:
        print(f"❌ json.loads 失败: {ex}")
        sys.exit(1)

    print(f"✅ json.loads 通过  ({fp})")
    errs = validate(data)
    if errs:
        print(f"❌ 契约校验未通过，{len(errs)} 项：")
        for x in errs:
            print("   -", x)
        sys.exit(1)

    print("✅ 契约校验通过（顶层字段 / 枚举值 / 映射规则 / 一票否决 全部合规）")
    print(f"   score={data['overall_score']}  verdict={data['verdict']}  "
          f"issues={len(data['issues'])} 条 "
          f"(严重 {sum(1 for i in data['issues'] if i['severity']=='严重')} / "
          f"建议 {sum(1 for i in data['issues'] if i['severity']=='建议')} / "
          f"微调 {sum(1 for i in data['issues'] if i['severity']=='微调')})")
