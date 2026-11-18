# GEO 工作台 · 桌面版使用说明

这是同一套 GEO 系统的 **Windows 单机形态**：双击即用，数据全部留在本机。
服务器部署（gunicorn + PostgreSQL）路径不受影响——桌面化只是新增一种运行形态，
`app/` 业务代码零改动。

---

## 快速开始

1. 把整个 `GEOArticlePipeline` 文件夹拷到任意**可写**目录（建议桌面或 D 盘根目录，
   **不要放 Program Files**——数据要写在 exe 旁边）。
2. 双击 `GEOArticlePipeline.exe`。
3. 浏览器自动打开 `http://127.0.0.1:7001`，已自动登录，直接使用。

关闭方式：关闭命令行窗口，或在窗口内按 Ctrl+C。

## 数据都在哪（可整目录备份/删除）

```
GEOArticlePipeline\
├─ GEOArticlePipeline.exe
├─ _internal\            ← 运行时与规格（只读，勿动）
└─ data\                 ← 你的全部数据（备份这个目录即可）
   ├─ app.db             ← 数据库（账号/品牌/标题/文章索引）
   ├─ credentials.txt    ← 本地账号口令（自动登录，一般用不到；忘记口令时查看）
   ├─ secret_key.txt     ← 会话密钥（删了会全员重新登录，无数据损失）
   ├─ logs\app.log       ← 运行日志（JSON 行，排障时看）
   └─ local\articles\    ← 文章产物（01_draft / 02_review / 03_final）
```

**删除 `data\` 再启动 = 完全重置**（自动重建空库与新账号，属幂等首启引导）。

## 接入真实 LLM（可选）

开箱即用内置离线假供应商（生成占位文本，用于验证流程）。要接真实模型：

1. 在 exe 旁新建 `.env` 文件（与 GEOArticlePipeline.exe 同目录）；
2. 写入：

   ```
   GEO_LLM_PROVIDER=deepseek
   GEO_LLM_KEYS=sk-你的Key
   ```

3. 重启程序。支持 `deepseek / doubao / qwen / openai-compat`（OpenAI 兼容端点）。

`.env` 存在时完全以它为准；没建 `.env` 时才默认离线模式。

## 端口被占用怎么办

默认用 7001，被占用时自动向上找一个空闲端口（浏览器会打开正确端口）。
想固定端口：设置系统环境变量 `GEO_PORT=8000` 后启动。

## 常见问题

| 现象 | 说明 |
|---|---|
| 双击没反应 / 一闪而过 | 用命令行进入目录执行 `GEOArticlePipeline.exe --selftest`，把输出截图排障 |
| SmartScreen / 杀软提示 | 未签名 exe 的常见误报；选"仍要运行"并加入白名单即可（发布物附 SHA256 可比对） |
| 提示"品牌资料「品牌概述」…阻塞" | 冻结规格要求先填品牌资料：到「品牌资料」页粘贴你的品牌介绍（首句须含品牌名），属正常产品流程 |
| 重复双击 | 不会起第二个实例——程序探测到旧实例后直接开浏览器复用 |
| 退出时任务还在跑 | 线程池会等当前 LLM 调用收尾（最长 60s），属正常 |
| 退出登录后又自动登录 | 桌面单机形态的有意行为；SaaS 服务器部署无此逻辑 |

## 自检命令（排障用）

```
GEOArticlePipeline.exe --selftest
```

一次性临时数据根上离线跑通 选题→初稿→审稿→定稿 全链路，末行输出
`[selftest] ... -> PASS` 即为健康。此命令**不会**碰你的真实数据。
