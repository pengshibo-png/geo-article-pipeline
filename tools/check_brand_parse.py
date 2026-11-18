"""验证品牌资料改后：主名解析是否正常 + 标题库里是否残留违规标题。"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.writer.derived import derive_brand_names, extract_phone  # noqa: E402

con = sqlite3.connect(ROOT / "data" / "app.db")
content = (con.execute("SELECT content FROM brand WHERE tenant_id='local'").fetchone() or [""])[0]

names = derive_brand_names(content)
print("品牌名解析:", names)
print("识别成功:", "✓" if names.get("ok") else "✗ 会阻塞生成！")
print("电话提取:", extract_phone(content))

BAD = ("非遗", "老字号", "国家级", "独家", "第一", "唯一", "顶级", "最好", "最佳",
       "一口价", "百分百", "永久", "零风险", "保证", "根治", "彻底")
rows = con.execute("SELECT id, text, used FROM title WHERE tenant_id='local'").fetchall()
print(f"\n标题库 {len(rows)} 条")
bad_rows = [(i, t, u) for i, t, u in rows if any(w in t for w in BAD)]
print("含违规词的标题:", len(bad_rows))
for i, t, u in bad_rows:
    print(f"  #{i} used={u}  {t}")
con.close()
