#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
品牌资料校验器 · check_brand.py
================================
对应《品牌资料九大分区规范》§7 门禁 B1–B6，以及 _共享/brand-names.md 的同源约束。

用法：
    python check_brand.py <品牌资料文件.txt|json>
    # .json 需含 content 字段；.txt 视为正文
    # 不带参数时，默认跑 _样例/示例品牌资料-聚贤阁珠宝.txt

退出码：0 = 全部通过；1 = 有阻断项（B2 主名提取失败 / B3 主体名不一致）
输出：
    [B1] 分区名与顺序一致性
    [B2] 品牌概述首句 → 主名派生
    [B3] 资质与认证 主体名称： 与主名一致性
    [B4] 逐区必含要素
    [B5] 硬编码风险扫描（电话 / 疑似品牌名 / 跨行业残留）
    [B6] 完整度：主指标（写全）+ 要素指标（写对）
"""
import sys, os, re, json

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = json.load(open(os.path.join(HERE, "brand-sections.json"), encoding="utf-8"))
SECTIONS = SPEC["sections"]
NAMES = [s["name"] for s in SECTIONS]

# 跨行业残留词（本系统为珠宝修复业务；发现他行业词即提示）
CROSS_INDUSTRY = ["漏水", "防水补漏", "免砸砖", "砸砖", "隐蔽工程", "施工队", "聚氨酯", "房顶"]


def load_text(path):
    raw = open(path, encoding="utf-8").read()
    if path.lower().endswith(".json"):
        return json.loads(raw).get("content", "")
    return raw


def parse(text):
    """复现原系统 parse()：行首 ^【…】 开块，首块前为 intro。"""
    raw, order, intro, cur = {}, [], [], None
    for ln in text.split("\n"):
        m = re.match(r"^【(.+?)】\s*$", ln)
        if m:
            cur = m.group(1)
            if cur not in raw:
                raw[cur] = []
                order.append(cur)
        elif cur is not None:
            raw[cur].append(ln)
        else:
            intro.append(ln)
    blocks = {t: "\n".join(v).strip() for t, v in raw.items()}
    return blocks, order, "\n".join(intro).strip()


def find_block(blocks, order, name, intro=""):
    """精确块 → 包含匹配（兼容【核心方法·技术·工艺】）→ 品牌概述兜底 intro（修正项 M1）。"""
    if name in blocks and blocks[name]:
        return blocks[name]
    for t in order:
        if name in t and blocks.get(t):
            return blocks[t]
    if name == "品牌概述" and (intro or "").strip():
        return intro.strip()
    return ""


def norm(s):
    return re.sub(r"[【】\s]", "", s or "")


def sec_filled_original(text, name):
    """原系统算法（用于对比展示缺陷）。"""
    i = text.find(name)
    if i < 0:
        return False
    return len(norm(text[i + len(name): i + len(name) + 60])) > 8


def derive_brand_names(blocks, order, intro):
    """按 _共享/brand-names.md §2 派生主名/别名。"""
    # ① 主名：品牌概述首句第一个标点前的部分
    overview = find_block(blocks, order, "品牌概述", intro)
    first = re.split(r"[，,。;；]", overview.strip(), maxsplit=1)[0].strip() if overview else ""
    main = first if first and len(first) <= 20 else ""
    # ② 别名-主体名：资质与认证 → 主体名称：
    qual = find_block(blocks, order, "资质与认证")
    m = re.search(r"主体名称[:：]\s*([^\n]+)", qual)
    entity = m.group(1).strip() if m else ""
    # ③ 别名-简称：显式声明
    alias = ""
    m2 = re.search(r"(?:简称|以下简称)[:：\"']?\s*[「\"']?([^\"」'，,。；;\n]{2,10})", overview)
    if m2:
        alias = m2.group(1).strip()
        alias_src = "显式声明"
    elif main:
        a = re.sub(r"^(上海|北京|广州|深圳|中国)", "", main)
        a = re.sub(r"(珠宝|科技|工程|服务|有限公司|有限责任公司)$", "", a)
        alias = a if a and a != main else ""
        alias_src = "启发式(待确认)" if alias else "-"
    else:
        alias_src = "-"
    return main, entity, alias, alias_src


def element_check(name, body):
    """粗略要素命中：按必含要素关键词做提示性校验（非严格）。"""
    hints = {
        "品牌概述": [r"是.*", r"(年|成立)"],
        "资质与认证": [r"主体名称[:：]", r"编号|代码|证书", r"查询|核验|公示"],
        "门店与服务网络": [r"(市|区|路|号|门店|店)", r"(覆盖|服务范围|全市|全域)", r"(寄修|上门|异地)"],
        "核心产品与服务": [r"[：:]", r"(适合|适用|针对)"],
        "价格体系": [r"\d+\s*[-~至]\s*\d+|\d+\s*元", r"(包含|含|免费|不收)"],
        "专业团队": [r"\d+\s*(名|人)", r"(\d+\s*年|从业)", r"编号|证书|持证"],
        "服务流程与承诺": [r"\d+[\.、]|[→>]", r"(质保|保\w*|售后)", r"(承诺|保证)"],
        "核心方法": [r"[：:]", r"[→>]|步骤|\d+[\.、]"],
        "品牌总结": [r".{20,}"],
    }
    pats = hints.get(name, [])
    hit = sum(1 for p in pats if re.search(p, body or ""))
    return hit, len(pats)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "_样例", "示例品牌资料-聚贤阁珠宝.txt")
    text = load_text(path)
    blocks, order, intro = parse(text)
    print(f"文件：{os.path.basename(path)}　正文字数(去空白)：{len(norm(text))}\n")

    # ---- B1 分区名与顺序 ----
    print("[B1] 分区名与顺序")
    missing = [n for n in NAMES if not find_block(blocks, order, n)]
    extra = [t for t in order if not any(n in t for n in NAMES)]
    print(f"     九大分区名前缀匹配：{'✅ 全部可识别' if not missing else '⚠️ 缺失 ' + str(missing)}")
    if extra:
        print(f"     额外/非标准分区块：{extra}")
    if order:
        print(f"     实际出现顺序：{' → '.join(order)}")

    # ---- B2 主名派生 ----
    main_name, entity, alias, alias_src = derive_brand_names(blocks, order, intro)
    print("\n[B2] 品牌概述首句 → 主名派生")
    ov = find_block(blocks, order, "品牌概述", intro)
    print(f"     首句：{ov.split(chr(10))[0][:60] if ov else '(空)'}…")
    print(f"     主名 = {main_name or '❌ 提取失败'}")
    ok_b2 = bool(main_name)
    if not ok_b2:
        print("     ❌ 主名提取失败 → 按 brand-names.md R2 应【阻塞生成】")

    # ---- B3 主体名称一致性 ----
    print("\n[B3] 资质与认证 `主体名称：` 一致性")
    print(f"     主体名称 = {entity or '(未找到)'}")
    ok_b3 = bool(entity) and entity == main_name
    if not entity:
        print("     ⚠️ 未找到 `主体名称：` 字段（brand-names.md §4 要求必含）")
    elif entity != main_name:
        print(f"     ❌ 与主名【{main_name}】不一致 → 按同源约束应【报错】")
    else:
        print("     ✅ 与主名一致")

    print(f"\n     派生结果：{main_name}（{alias or '无简称'}{' · ' + alias_src if alias else ''}）")

    # ---- B4 逐区要素 ----
    print("\n[B4] 逐区必含要素（提示性）")
    elem_hit_total = elem_all_total = 0
    for s in SECTIONS:
        body = find_block(blocks, order, s["name"], intro)
        hit, tot = element_check(s["name"], body)
        elem_hit_total += hit
        elem_all_total += tot
        flag = "✅" if hit == tot else ("🟡" if hit else "❌")
        print(f"     {flag} {s['name']:8s} 要素 {hit}/{tot}　正文 {len(norm(body)):4d} 字")

    # ---- B5 硬编码扫描 ----
    print("\n[B5] 硬编码风险扫描")
    phones = re.findall(r"1[3-9]\d{9}", text)
    cross = [w for w in CROSS_INDUSTRY if w in text]
    print(f"     电话号：{len(phones)} 处 {'（应抽为 {phone} 变量，不得进 Prompt）' if phones else ''}")
    print(f"     跨行业残留词：{cross if cross else '✅ 无'}")

    # ---- B6 完整度 ----
    print("\n[B6] 完整度")
    filled_orig = sum(1 for s in SECTIONS if sec_filled_original(text, s["name"]))
    filled_fix = sum(1 for s in SECTIONS if len(norm(find_block(blocks, order, s["name"], intro))) >= s["min_chars"])
    pct_orig = round(filled_orig / len(SECTIONS) * 100)
    pct_fix = round(filled_fix / len(SECTIONS) * 100)
    pct_elem = round(elem_hit_total / elem_all_total * 100) if elem_all_total else 0
    print(f"     原算法（含缺陷 D-1/D-4）：{pct_orig}%　({filled_orig}/9)")
    print(f"     修正算法（阈值判定）：　　{pct_fix}%　({filled_fix}/9)")
    print(f"     要素完整度（写对程度）：　{pct_elem}%　({elem_hit_total}/{elem_all_total})  ← 副指标")
    if filled_orig != filled_fix:
        miss = [s["name"] for s in SECTIONS
                if sec_filled_original(text, s["name"]) != (len(norm(find_block(blocks, order, s["name"], intro))) >= s["min_chars"])]
        print(f"     口径差异分区：{miss}（原算法缺陷 D-1）")

    print("\n" + "=" * 56)
    ok = ok_b2 and ok_b3
    print("门禁结论：", "✅ B1–B6 通过（主名/主体名一致）" if ok else "❌ 阻断：主名提取失败 或 主体名称不一致")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
