"""桌面形态（Windows 单机版）：launcher 入口 + 首启引导 + 自动登录 + 冒烟自检。

这是**新增的一种运行形态**，不是替代：SaaS 服务器路径（gunicorn + alembic）不受
影响，app/ 包零改动。数据与规格的路径决策全部集中在 desktop.paths。
"""
