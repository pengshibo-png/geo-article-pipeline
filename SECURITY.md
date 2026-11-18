# 安全说明（SECURITY.md）

## 密钥与敏感数据的存放约定

- **运行期一切密钥都在仓库之外**：`.env`（`GEO_SECRET_KEY`、`GEO_LLM_KEYS` 等）与
  `data/model_config.json`（设置页写入的模型 Key、蚁小二 token）均已被 `.gitignore`
  排除。本仓库**不含任何真实密钥**，也不含任何客户运行数据。
- **平台账号 Cookie** 一律 Fernet 静态加密后入库（`app/services/crypto.py`），
  接口回显只给掩码（`mask_key`），永不回传明文。
- **多租户隔离**：租户上下文只来自会话；仓储层强制注入过滤 + `before_flush` 兜底；
  生产建议启用 PostgreSQL RLS（迁移已内置策略，事务级租户上下文见 `tenancy.py`）。

## 报告分享链接

`/r/<token>` 为 token-即-授权的匿名链接：链接本身没有访问控制，
**任何拿到 token 的人都可读**。不需要对外分享时请使用列表页的「撤回」。

## 已知需要自行加固的点（v0.x）

1. 生产务必设置强 `GEO_SECRET_KEY`（dev 默认值会被 prod 配置层直接拒绝启动）；
2. 反向代理后请为会话 Cookie 配置 `Secure` / `SameSite`（计划内，见 README 待办）；
3. 公开注册端点目前未开放；未来开放前需加验证码/审批（见 README 待办）；
4. 登录限流按账号维度，公网部署建议再叠一层网关限流。

## 报告漏洞

发现安全漏洞请开 GitHub Issue（公开）或直接联系维护者（私密）。
请勿在公开 Issue 中粘贴可复现的凭证或真实数据。
