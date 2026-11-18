# GEO 内容营销流水线 · 开源版（article-pipeline）

[![CI](https://github.com/pengshibo-png/geo-article-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/pengshibo-png/geo-article-pipeline/actions/workflows/ci.yml)

多租户 **GEO（生成式引擎优化）内容营销系统**：选题 → AI 写作 → 审稿修改 → 配图 →
多账号分发 → AI 引擎监测 → 效果报告，一条龙自托管。

技术栈：Python 3.12 + Flask + Jinja2 服务端渲染 + 原生 JS + ECharts + SQLAlchemy /
Alembic（开发用 SQLite，生产支持 PostgreSQL + 行级安全 RLS）。零前端构建链。

> **开箱即可离线跑通**：默认 `GEO_LLM_PROVIDER=fake`（内置确定性假供应商），
> 不配任何 API Key 也能把「选题 → 写作 → 审稿 → 修改 → 发布（假驱动）→ 监测 →
> 报告」全链路走一遍，任务日志会如实标注哪些环节不是真实调用。

English note: a self-hosted, multi-tenant GEO (Generative Engine Optimization)
content-marketing pipeline — topic mining, AI writing/review/revision, images,
multi-account publishing, AI-engine monitoring and reports. Docs are zh-CN first.

---

## 快速开始

```bash
# 1) 依赖（项目级 venv，隔离于系统环境）
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt     # Linux/macOS

# 2) 配置
cp .env.example .env        # 按需修改；生产必须设置 GEO_SECRET_KEY

# 3) 建表（生产一律用迁移；本地开发可用 create_all）
./.venv/Scripts/python.exe -m alembic upgrade head
# 或不装 alembic 直接建表：GEO_INIT_DB=1 ./.venv/Scripts/python.exe app.py

# 4) 跑起来（开发服务器，仅本地）
./.venv/Scripts/python.exe app.py           # http://127.0.0.1:7001/healthz

# 5) 跑门禁（S3 规格 + 工程门禁 + 全量测试）
./.venv/Scripts/python.exe tools/ci.py
```

**生产启动**（ADR-012：禁用 Werkzeug 开发服务器）：

```bash
gunicorn "app:create_app()" -w 4 -b 0.0.0.0:8000
```

**接入真实模型**：系统设置页支持任意 OpenAI 兼容端点（base_url + model + key，
保存即生效、界面只回显掩码），或直接在 `.env` 配 `GEO_LLM_PROVIDER` / `GEO_LLM_KEYS`。
密钥只存在于你的 `.env` 与运行期 `data/`（两者都被 `.gitignore` 排除），**本仓库不含
任何真实密钥**。

---

## 目录结构

```
article-pipeline/
├─ app.py                     # WSGI 入口（≤60 行，只装配）
├─ app/
│  ├─ __init__.py             # create_app()（≤120 行）
│  ├─ config.py               # 配置分层 + fail fast（时区强制 Asia/Shanghai，ADR-013）
│  ├─ logging.py              # 结构化 JSON 日志 + request_id / tenant_id 透传（ADR-010）
│  ├─ errors.py               # 类型化错误层级 + 全局处理器
│  ├─ tenancy.py              # ★ 多租户隔离四道防线（ADR-003）
│  ├─ security.py             # 会话鉴权 / 登录限流（按账号维度）
│  ├─ timeutil.py             # Asia/Shanghai（Windows 无 IANA 时区时自动降级 UTC+8）
│  ├─ spec.py                 # S3 冻结规格的定位入口（默认仓库内 spec/，可用 GEO_SPEC_DIR 覆盖）
│  ├─ models/                 # 23 张表，按域拆文件
│  ├─ blueprints/             # 6 业务蓝图 + 1 平台层（auth/console/config/topic/generation/distribution/insight）
│  ├─ services/               # llm（多 Key 池）· writer（A→B→C 流水线 + 真模型输出清洗 + managed 状态桥）· publish（渠道驱动）
│  ├─ platform/               # jobs（队列抽象）· storage（租户路径）
│  └─ templates/              # 22 页服务端渲染（未接域页面走 404→空态）
├─ spec/                      # S3 内容规格冻结：四段 Prompt（T/A/B/C）+ 变量注册表 + 品牌资料九大分区规范
├─ alembic/                   # 迁移（含 PostgreSQL RLS 策略，SQLite 自动跳过）
├─ tools/                     # ci.py（门禁）· gen_blueprints.py（脚手架）· real_e2e.py（真实 LLM 全链路验证）
├─ tests/                     # unit / contract / isolation / E2E
├─ desktop/                   # 桌面形态打包（PyInstaller 单目录 exe，开箱即用）
└─ data/                      # 运行期数据（按租户隔离；不入版本库，首次启动自动创建）
```

---

## 核心设计

| 约束 | 落点 | 验证 |
|---|---|---|
| **多租户四道防线** | `tenancy.py`：D1 上下文（租户只来自 session）/ D2 仓储强制注入 + `before_flush` 兜底 / D3 PG RLS（迁移生成）/ D4 复合唯一键 + `tenant_path()` | `tests/isolation/` V1–V6 |
| **越权读返回 404 而非 403** | `TenantScopedRepository.get_or_404` | V4 |
| **admin 跨租户业务域只读** | `before_flush` + `admin_scope(reason=)` 强制 | V3 / A18 |
| **行数预算** | `app.py` ≤60 · `create_app()` ≤120 · 单文件 ≤300 | `tests/contract/` A1/A3 |
| **依赖方向** | `views → service → repository → model`；services 不依赖 blueprints、platform 不依赖 services | A2 |
| **LLM 多 Key 池** | 三态状态机 + 冷却自动复活 + 加权轮询 + 全冷却排队 + 限流双识别 | `tests/unit/test_keypool.py` |
| **四段 Prompt 冻结契约** | `spec/` 的 T/A/B/C 模板 + 变量注册表唯一来源，`check_variables.py` 机器校验（V1–V4） | `tools/ci.py` 门禁 |
| **B 段审稿输出 JSON 契约** | 纯 JSON / 枚举 / verdict↔score 映射 / location ∈ 原文，`validate_review.py` 把关 | `tests/unit/test_review_pipeline.py` |
| **品牌名零硬编码** | `{brand_names}` 从品牌资料九大分区派生（`spec/_共享/brand-names.md`），Prompt 只引用不复制 | V4 + `check_brand.py` |
| **Cookie 静态加密** | 账号 Cookie 一律 Fernet 加密落库，绝不明文 | `services/crypto.py` |
| **断点续跑** | 检查点 = `01_draft.html / 02_review.json / 03_final.html` 是否落盘 | `tests/test_pipeline_e2e.py` |

开发历程（Phase 0 → 4.6 的功能接线记录）保留在下方「Phase 进度」一节，
可当作各子系统的设计说明来读。

---

## 测试与门禁

```bash
./.venv/Scripts/python.exe -m pytest tests -q      # 全量测试（假供应商，离线可跑）
./.venv/Scripts/python.exe tools/ci.py             # 4 道门禁串联（S3 规格 + 工程 + 测试）
```

**真实 LLM 全链路验证**（需先在系统设置页配好真实模型）：

```bash
./.venv/Scripts/python.exe tools/real_e2e.py --titles 3   # T→A→B→C 实跑 + 输出兼容性诊断
```

- **V5（PG RLS 冒烟）默认 skip** —— SQLite 不支持 RLS，按 ADR-005 必须在 PostgreSQL 上执行。
- 迁移回滚验证：`alembic upgrade head` → `alembic downgrade base`（表全部撤除）。

---

## 发布渠道与合规提醒

- **内置两类驱动**：① 本地浏览器适配器（Playwright：搜狐号/百家号/美篇/列表网/58 同城，
  Cookie 授权，选择器可用 `data/publish_selectors.json` 热覆盖）；② 可选接入第三方
  多平台发布工具（蚁小二 CLI/API，系统设置页配 Key）。都没配时走**离线假驱动**，
  任务日志如实标注"不会真的发出去"——**任何情况下都不会假装发布成功**。
- 抖音图文 / 视频产线经第三方工具或人工渠道分发，本站生成内容与内容包。
- ⚠️ 自动化发布请遵守目标平台的服务条款与频率限制，因违规使用产生的后果由使用者自负。

---

## Phase 进度（开发历程存档）

| Phase | 内容 | 状态 |
|---|---|---|
| **Phase 0** | 应用工厂 / 配置分层 / 结构化日志 / 错误处理 / 6+1 蓝图 / 模型 + Alembic 迁移 / 租户隔离 + V1–V6 | ✅ |
| **Phase 1** | LLM 多 Key 池 / T→A 生成闭环 / 落盘 `01_draft.html` / 断点续跑检查点 | ✅ |
| **Phase 2** | B（审稿）→ C（修改）闭环：`02_review.json` 冻结契约校验 / verdict 分流 / `03_final.html` / 全流程任务 | ✅ |
| **Phase 3** | 22 页前端 + 核心流程：选题库 API、批量写稿 + 进度、文章档案、`/api/me` + 租户目录、生成计划、发布渠道组装（虚拟聚合渠道）、桌面 exe + 冒烟 | ✅ |
| **Phase 3.5** | 发布执行器（状态机 + 幂等 + 原子扣额度）、任务中心、账号授权（Cookie Fernet 加密）、GEO 监测执行器（采样 → 日聚合 → 看板）、报告中心、素材上传、自定义 AI 指令、标题池自动补货、定时调度、一键开通租户 | ✅ |
| **Phase 3.6** | 任意 OpenAI 兼容模型/生图端点在线接入（测试连接 + Key 掩码）、蚁小二多平台一键发布（云账号库/勾选同步/自动路由/演示模式）、抖音图文产线（16 端点：LLM 文案 + 生图 + 内容包库 + 质检 + 发布账本） | ✅ |
| **Phase 3.7** | 真模型全链路验证与输出兼容加固（围栏/寒暄/编号清洗统一进 `output_clean.py`；B 段分数类型归一） | ✅ |
| **Phase 3.8** | 蚁小二 CLI 内容发布（`publish init` 模板 → 封面三级回退 → 云发布 → task_set_id；错误信封归一） | ✅ |
| **Phase 3.9** | 真实渠道驱动（Playwright 五适配器：声明式选择器 + Cookie 三格式 + 登录态/验证码判定 + 失败现场截图 + 选择器热覆盖） | ✅ |
| **Phase 3.10** | 报告分享链接 `/r/<token>`、三类 CSV 导出（防注入）、运营端开通租户表单、AI 短视频产线（分镜 → ffmpeg 合成 → 发布账本，未配供应商时 mock 如实标注） | ✅ |
| **Phase 3.11** | 前端契约错配修复（报告 `share` 嵌套对象、看板 `sov` 卡） | ✅ |
| **Phase 4.1** | GEO 数据准确性：被引判定以真实发布 URL 为准；四引擎独立端点（未配退回统一模型并如实提示） | ✅ |
| **Phase 4.2** | 同行挖掘（LLM 推演，不爬外部站点）与客户画像 AI 洞察两个异步任务接真 | ✅ |
| **Phase 4.3** | 抖音循环发布调度真触发；报告配额自然月重置 | ✅ |
| **Phase 4.4** | PG 行级安全事务级上下文（`set_config('app.tenant_id', …)`） | ✅ |
| **Phase 4.5** | 去重上限 / 原子抢占防双开 / 被引 O(1) 精确匹配 / 配额原子扣减 | ✅ |
| **Phase 4.6** | 有界状态桥（防无界增长）、自研零依赖 CSRF（令牌跨登录存活）、发布账本加锁 | ✅ |
| Phase 4+ | 真 PDF、Redis+RQ 队列、竞品对比页接真、审计可视化、文件存储 GC、公开注册风控、会话 Cookie Secure/SameSite | 待办 |

---

## 已知边界（诚实声明）

1. **发布渠道**：`services/publish/platforms/` 五个浏览器适配器为**特定时期各平台页面的
   实测选择器，站点改版会失效**——用 `tools/publish_probe.py --platform <平台> --account
   <账号id> --headed` 现场校准，结果写 `data/publish_selectors.json` 热覆盖即可，
   不必改代码。平台侧风控（验证码等）需人工介入。
2. **GEO 四引擎**（豆包/通义/元宝/DeepSeek）：未逐个配独立端点时共用同一模型，
   引擎间差异仅供参考。
3. **发布批次进度**为进程内状态桥（单进程部署有效），重启即清零（数据不丢，在库在盘）。
4. **`/api/report/pdf` = 打印版 HTML**（浏览器打印另存）；真 PDF 依赖 weasyprint
   系统库，未接。分享链接 `/r/<token>` 匿名可访（token 即授权）。
5. **账号「验证」按钮是标记性的**；真实登录态校验发生在发布时（跳登录页 →
   `login_expired` → 账号置 `invalid`）。
6. **RLS 的 PG 部署**需要为登录前查询（按用户名查用户）安排 BYPASSRLS 角色或专用连接。
7. 本仓库为开源实现：**不含任何真实客户数据、密钥或运行期 `data/`**；
   `spec/` 与示例中的品牌「聚贤阁」均为虚构演示数据。

---

## 参与贡献

1. 改动四段 Prompt / 变量 → 先改 `spec/prompts/_共享/variable-registry.json` 与
   `变量注册表.md`，再改模板，跑 `spec/prompts/_共享/check_variables.py`；
2. 提 PR 前跑 `./.venv/Scripts/python.exe tools/ci.py`（4 道门禁全绿）；
3. 新增租户数据类文件请确认会被 `data/` 的 gitignore 规则覆盖，**切勿提交密钥**。

安全事项见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)
