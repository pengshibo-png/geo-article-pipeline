"""领域服务层：不依赖 Flask、不 import blueprints（依赖方向由 CI 强制）。

子域：llm（Key 池）/ writer（A→B→C 流水线）/ imagegen / publisher / geom / reporter。
Phase 0/1 只落地 llm 与 writer。
"""
