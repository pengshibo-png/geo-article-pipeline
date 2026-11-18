"""抽查合规清洗对真实残留的处理效果（号码删除后不留断句/空标签）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.writer.compliance import sanitize

CASES = [
    "<p>电话：13800001111，这个号白天有人接。现在就拨 13800001111。</p>",
    "<p><strong>13800001111</strong> 咨询。</p>",
    "<p>官方渠道约到店勘验或者上门，电话：13800001111。</p>",
    "<p>加微信 zhangsan_2026 发图，QQ：88886666。</p>",
    '<p><a href="tel:13812345678">点此联系</a></p>',
    "<p>行业第一，效果最好，100% 无痕，永不脱落。</p>",
]

for h in CASES:
    out, hits = sanitize(h, contact_mode="revise")
    print("原文:", h)
    print("清洗:", out)
    print("命中:", hits)
    print()
