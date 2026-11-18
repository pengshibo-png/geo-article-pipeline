"""展示品牌资料里新增的「门店地址」与「工艺参数」段落（核对写入结果）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
con = sqlite3.connect(ROOT / "data" / "app.db")
c = (con.execute("SELECT content FROM brand WHERE tenant_id='local'").fetchone() or [""])[0]
con.close()
print("总字数:", len(c))
for key in ("门店地址（两家直营实体门店）", "工艺条件（行业通行区间"):
    i = c.find(key)
    if i < 0:
        print(f"\n[{key}] 未找到")
        continue
    print(f"\n=== {key} ===")
    print(c[i:i + 420].split("\n\n")[0])
