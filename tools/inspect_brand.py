"""查看当前租户的品牌资料，核对"审稿要求的信息量"与"资料实际信息量"是否匹配。"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
con = sqlite3.connect(ROOT / "data" / "app.db")
row = con.execute("SELECT content FROM brand WHERE tenant_id='local'").fetchone()
con.close()
c = (row[0] if row else "") or ""
print("品牌资料字数:", len(c))
print("--- 全文 ---")
print(c[:2200])
print("\n--- 关键信息体检（审稿要求 vs 资料是否具备）---")
checks = {
    "具体门店地址(区+路+号)": bool(re.search(r"[区县].{0,8}[路街道].{0,8}\d", c)),
    "工艺参数(温度/湿度)": any(k in c for k in ("温度", "湿度", "℃")),
    "工艺参数(道数/目数/时长)": any(k in c for k in ("道", "目", "小时")),
    "资质/证书编号": bool(re.search(r"(证书|资质|鉴定师|GTC|证书编号|信用代码)", c)),
    "质保条款": any(k in c for k in ("质保", "保期", "保修")),
    "非遗/老字号表述": any(k in c for k in ("非遗", "老字号", "国家级")),
}
for k, v in checks.items():
    print(f"  {'有' if v else '缺'}  {k}")
print("\n门店数量表述:", re.findall(r"[一二两三0-9]+家(?:门店|店)", c) or "无")
