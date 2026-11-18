#!/usr/bin/env python3
"""四段 Prompt 变量注册表一致性校验（S3 共享规范 · 可接入 CI）

校验内容：
  V1  四段 Prompt 实际使用的变量 ⊆ 注册表            （防"野变量"）
  V2  注册表中该段的必填变量 ⊆ Prompt 实际使用        （防漏注入）
  V3  各段变量数量与注册表登记数一致                  （防口径漂移）
  V4  硬编码扫描：电话 / 年份 / 租户品牌名 / 他方品牌 / 跨行业词
      —— 对应门禁 G3（已按遗留项 2 补入"品牌名"一类）

不做扫描的范围：`_共享/` 下的规范文件（其中 brand-names.md 含品牌名派生示例，属正常）

用法：
    python check_variables.py            # 默认从脚本所在目录向上定位 prompts/
    python check_variables.py <prompts_dir>
退出码：0 = 全部通过；1 = 存在问题
"""
import json
import pathlib
import re
import sys

# ---------------- 定位目录 ----------------
HERE = pathlib.Path(__file__).resolve().parent          # .../prompts/_共享
PROMPTS_DIR = HERE.parent if HERE.name == "_共享" else HERE
if len(sys.argv) > 1:
    PROMPTS_DIR = pathlib.Path(sys.argv[1])

REGISTRY = HERE / "variable-registry.json"
SEGMENT_FILE = {
    "T": "T-标题选题.md",
    "A": "A-写文章.md",
    "B": "B-审稿质检.md",
    "C": "C-修改文章.md",
}
VAR_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")           # 只认 ASCII 小写变量名
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

# ---------------- 门禁 G3 硬编码模式（含新增的"品牌名"一类）----------------
HARDCODE = {
    "电话号":   r"1[3-9]\d{9}|\d{3}-\d{4}-\d{4}|400-\d{3}-\d{4}",
    "年份":     r"20\d{2}\s*年",
    "租户品牌名": r"聚贤阁|陈师傅",
    "他方品牌名": r"竞品甲",
    "跨行业词":  r"漏水|防水补漏|免砸砖|砸砖|隐蔽工程|翡翠|和田玉|珠宝修复",
}


def load_registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def used_vars(path: pathlib.Path) -> set[str]:
    """取 Prompt 正文实际使用的变量（剔除作者注释里的清单，避免自证污染）"""
    body = COMMENT_RE.sub("", path.read_text(encoding="utf-8"))
    return set(VAR_RE.findall(body))


def main() -> int:
    reg = load_registry()
    by_seg: dict[str, list[dict]] = {s: [] for s in SEGMENT_FILE}
    all_names: set[str] = set()
    for v in reg["variables"]:
        all_names.add(v["name"])
        for s in v["segments"]:
            by_seg.setdefault(s, []).append(v)

    errors: list[str] = []
    print("=" * 74)
    print("四段 Prompt 变量注册表一致性校验")
    print(f"注册表: {REGISTRY}")
    print(f"Prompt: {PROMPTS_DIR}")
    print("=" * 74)

    total_used: set[str] = set()

    for seg, fname in SEGMENT_FILE.items():
        fp = PROMPTS_DIR / fname
        if not fp.exists():
            errors.append(f"[{seg}] 文件不存在: {fp}")
            continue
        used = used_vars(fp)
        total_used |= used
        declared = {v["name"] for v in by_seg[seg]}
        required = {v["name"] for v in by_seg[seg] if v.get("required")}

        print(f"\n── {seg} 段 · {fname}")
        print(f"   注册表登记 {len(declared):2d} 个 | Prompt 实际 {len(used):2d} 个")

        # V1 野变量
        wild = used - all_names
        if wild:
            errors.append(f"[{seg}] V1 出现未登记的变量: {sorted(wild)}")
            print(f"   ❌ V1 未登记变量: {sorted(wild)}")
        else:
            print("   ✅ V1 无未登记变量")

        # V2 必填缺失
        miss = required - used
        if miss:
            errors.append(f"[{seg}] V2 必填变量未注入: {sorted(miss)}")
            print(f"   ❌ V2 必填未注入: {sorted(miss)}")
        else:
            print("   ✅ V2 必填变量齐全")

        # V2b 该段不该出现的变量（登记归属别的段，且本段也没用到）
        not_mine = used - declared
        if not_mine:
            errors.append(f"[{seg}] 变量归属不符（注册表未登记该段）: {sorted(not_mine)}")
            print(f"   ❌ 归属不符: {sorted(not_mine)}")
        else:
            print("   ✅ 段落归属一致")

        # V3 数量一致（Prompt 实际 vs 注册表登记）
        if len(used) != len(declared):
            errors.append(f"[{seg}] V3 数量不一致: Prompt {len(used)} vs 注册表 {len(declared)}")
            print(f"   ❌ V3 数量不一致（Prompt {len(used)} / 注册表 {len(declared)}）")
        else:
            print(f"   ✅ V3 数量一致（{len(used)}）")

    # 全局变量总数
    print(f"\n── 全局\n   注册表唯一变量 {len(all_names)} 个 | 四段实际合计 {len(total_used)} 个")
    if len(total_used) != len(all_names):
        errors.append(f"[全局] V3 唯一变量数不一致: 实际 {len(total_used)} vs 注册表 {len(all_names)}")
        print("   ❌ 唯一变量数不一致")
    else:
        print("   ✅ 唯一变量数一致")

    # V4 硬编码扫描
    print("\n── V4 硬编码扫描（门禁 G3，含品牌名一类）")
    for seg, fname in SEGMENT_FILE.items():
        fp = PROMPTS_DIR / fname
        if not fp.exists():
            continue
        body = COMMENT_RE.sub("", fp.read_text(encoding="utf-8"))
        hits = {k: sorted(set(re.findall(p, body))) for k, p in HARDCODE.items()}
        hits = {k: v for k, v in hits.items() if v}
        if hits:
            errors.append(f"[{seg}] V4 硬编码命中: {hits}")
            print(f"   ❌ {fname}: {hits}")
        else:
            print(f"   ✅ {fname}: 未命中")

    # 汇总
    print("\n" + "=" * 74)
    if errors:
        print(f"❌ 校验未通过，共 {len(errors)} 项问题：")
        for e in errors:
            print("   -", e)
        return 1
    print("✅ 全部通过（V1 野变量 / V2 必填注入 / V3 数量一致 / V4 硬编码）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
