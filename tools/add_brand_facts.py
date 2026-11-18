"""给品牌资料补上「门店地址」与「工艺参数」（2026-10-22 联网核实后写入）。

**事实来源（交叉验证）**
1. 官网 www.shzbxf.net/contact：
   奉贤主店 上海市奉贤区奉城镇城大路199弄2号
   浦东分店 上海市浦东新区高桥镇江东路1726弄25号7幢101室
   营业时间 09:00-18:00，联系电话 13800001111
2. 鲨鱼博客《上海聚贤阁工坊探访》：同样的两店地址；并提到
   「珠宝级环氧树脂固化温度建议 25-30℃，固化时间 24 小时」
3. 58同城店铺页：地址与**统一社会信用代码 91310000MA0K8XQ23L**
   —— 与品牌资料里的信用代码一致，可确认是同一主体

**大漆/金缮工艺参数**（多个公开来源的共同区间，非单一来源）：
- 荫房（阴干房）：温度 25-30℃、相对湿度 75-85%
  （石破天紫砂 25-30℃/75-85%；今日头条 25℃+/75%；人民日报 25℃/80%）
- 每道髹涂间隔 12-24 小时；金缮通常上漆 3-7 道；阴干周期约 7-10 天
- 打磨：400 目起逐级细磨，再抛光

所有参数均标注「具体以实物勘验为准」，不做绝对承诺。

用法：.venv\\Scripts\\python.exe tools\\add_brand_facts.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OLD_STORE_LINE = "门店口径：上海两家直营实体门店（具体地址与营业时间以官方渠道确认为准）；外地客户可寄修"
# 3 处「门店口径」统一改成引用式短句，完整地址只在【门店与服务网络】下写一次：
# 地址重复三遍会让模型在正文里反复罗列门店信息。
NEW_STORE_LINE = "门店：上海两家直营实体门店（地址与营业时间见【门店与服务网络】）；外地客户可寄修"
ADDR_BLOCK = (
    "门店地址（两家直营实体门店）：\n"
    "▪ 奉贤主店：上海市奉贤区奉城镇城大路199弄2号\n"
    "▪ 浦东分店：上海市浦东新区高桥镇江东路1726弄25号7幢101室\n"
    "营业时间：09:00-18:00（两家门店均可到店看货、现场勘验；外地客户可寄修）"
)

OLD_TAIL = "▶ 其他实际工艺：金缮修缮、锔钉加固、缺口补配、圈口调整、抛光翻新、多段拼接（工艺要点见上文对应条目）。"
NEW_TAIL = OLD_TAIL + """

▶ 工艺条件（行业通行区间，具体以实物勘验与现场工艺卡为准）：
- 微晶融合（珠宝级环氧类基材）：固化温度 25-30℃，固化时间约 24 小时；
- 金缮（天然大漆）：荫房温度 25-30℃、相对湿度 75-85%，每道髹涂间隔 12-24 小时，
  通常髹涂 3-7 道，阴干周期约 7-10 天；
- 打磨：从 400 目起逐级细磨后再抛光，描金前需逐级打磨至表面平整。"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "app.db")
    row = con.execute("SELECT content FROM brand WHERE tenant_id='local'").fetchone()
    old = (row[0] if row else "") or ""
    if not old:
        print("品牌资料为空，终止")
        con.close()
        return 1

    n_store = old.count(OLD_STORE_LINE)
    print(f"门店口径行命中: {n_store} 处（应为 3）")
    if n_store == 0:
        print("未匹配到预期门店口径行，终止（避免误改）")
        con.close()
        return 2
    new = old.replace(OLD_STORE_LINE, NEW_STORE_LINE)
    if "【门店与服务网络】" not in new:
        print("未找到【门店与服务网络】小节，终止")
        con.close()
        return 2
    new = new.replace("【门店与服务网络】\n", "【门店与服务网络】\n" + ADDR_BLOCK + "\n", 1)

    if OLD_TAIL not in new:
        print("未匹配到工艺段落结尾行，终止（避免误改）")
        con.close()
        return 2
    new = new.replace(OLD_TAIL, NEW_TAIL)

    print(f"字数 {len(old)} → {len(new)}")
    print("新增地址:", "城大路199弄2号" in new and "江东路1726弄25号" in new)
    print("新增参数:", "荫房温度 25-30℃" in new)
    if not a.apply:
        print("\n（预览模式；加 --apply 生效）")
        con.close()
        return 0

    bak = ROOT / "data" / "brand.before-facts.json"
    bak.write_text(json.dumps({"content": old}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    con.execute("UPDATE brand SET content=? WHERE tenant_id='local'", (new,))
    con.commit()
    con.close()
    print(f"\n已写库；改前备份：{bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
