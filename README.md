# 财税连续办理助手

按 PRD / SPEC 1.2 实现的本地比赛工程。后端 Python 3.12 + FastAPI，前端 TypeScript + React，数据库 PostgreSQL 16。所有企业、票据与规则都是比赛模拟资料。

业务由同一个模型工具循环办理：查询月度资料、读取适用规则、检查并补充发票用途/备注。模型决定如何理解与追问；程序执行权限、精确计算、保存、核验、幂等和版本校验。生产代码没有按用户关键词选择固定业务答复。

记忆分为不可覆盖的来源、带修订的正式任务/显式偏好、派生历史索引/摘要。任务按用户与企业恢复；旧统计和旧规则不能替代当前查询。删除会使普通召回与派生摘要失效，保留核对审计；没有宣称全面擦除原文。

## 当前状态

后续优先级与可执行验收清单见[剩余 TODO（2026-10-31）](docs/PATCH002_TODO.md)。

第1项的真实长上下文已补充分批压缩、累计来源、最终守卫和实际调用预算验证，见[执行报告](docs/PATCH002_A_EXECUTION_REPORT.md)。原生推理崩溃本轮未复现，根因与日期分页漏游标仍保留为开放问题；不把零故障样本当作已根治。

已实现数据库及工具、任务/偏好管理、上下文预算与摘要、安全保存、租约恢复、规则 CLI、工作台和追溯界面，以及独立扩展实例中的 Excel 发票追加。验证记录见 [实施记录](docs/IMPLEMENTATION.md)、[测试报告](docs/TEST_REPORT.md) 与 [追加功能验证](docs/PATCH001_TEST_REPORT.md)。

整改分支已增加文本知识资料的私有上传、真实混合检索、持久引用、换版与停用，并接入企业维护发布和规则来源校验；本批补充开发/留出评测、校准弃答和完整条款组引用。见[知识接口](docs/PATCH002_KNOWLEDGE_CONTRACTS.md)、[安装与运行](docs/PATCH002_KNOWLEDGE_RUNTIME.md)、[接入报告](docs/PATCH002_M3_EXECUTION_REPORT.md)及[质量报告](docs/PATCH002_M4_EXECUTION_REPORT.md)。整改预览位于`http://127.0.0.1:8105/`，工作区`D:\tax-harness-remediation`。

第二批已增加历史真实语义召回、无关键词日期分页、长原话续读、来源失效与动态引文，见[历史契约](docs/PATCH002_HISTORY_CONTRACTS.md)及[执行报告](docs/PATCH002_M5_EXECUTION_REPORT.md)。后端249项、真实DeepSeek原40例单轮自动检查及历史Agent补验8项通过，8105已备份迁移后更新；ScriptedModel仅用于明确标记的程序测试。

凭据只在本地`.env`配置，仓库不包含密钥。合成制度完整证据47/48、1条口语误拒保留；合成历史留出20条正例、4条无答案及6项边界通过。Windows本地推理原生崩溃已通过隔离和有界重试控制影响，根因未明。真实强制长上下文已有本轮合成验证；OCR、完整故障矩阵、更广场景与人工语义签收、最终参赛验收仍未完成，不能将当前工程称为已全部验收。

## 启动

在 Windows PowerShell 中，前置环境为 Python 3.12+、Node.js 22.12+、npm。项目目录默认为 `D:\tax-harness`。

```powershell
cd D:\tax-harness
.\scripts\setup.ps1 -Python python -DatabaseMode native
.\scripts\start.ps1
```

初始化脚本创建虚拟环境、安装锁定依赖、生成随机本地数据库密码、安装项目内的 PostgreSQL 16.15、创建 schema 并导入比赛样例、构建前端。项目内数据库端口为 54337，不安装系统服务。便携数据库约需下载 330 MB，源码交付不包含它。

已有可用 Docker 时可用 `-DatabaseMode docker`。两种数据库方式占同一项目端口，择一使用。普通启动只检查 schema，不导入或覆盖 Excel。

打开 `http://127.0.0.1:8100`。U001 可查询与补充甲、乙企业；U002 仅能查询甲企业。身份选择是赛题模拟登录，页面已标注模拟环境，不是生产身份认证。服务默认只监听本机。

停止服务用终端 Ctrl+C。停止项目数据库：

```powershell
.\.venv\Scripts\python.exe scripts/native_postgres.py stop
```

## 配置真实模型

本地编辑 `.env`，不要将密钥发到聊天或提交 Git：

```dotenv
MODEL_BASE_URL=https://your-provider.example/v1
MODEL_ID=your-model-id
MODEL_API_KEY=your-local-key
MODEL_CONTEXT_LIMIT=your-provider-documented-token-limit
MODEL_EXTRA_BODY={}
```

接入标准 Chat Completions / function tools 协议。`MODEL_BASE_URL` 是到 `/v1` 的基址；程序追加 `/chat/completions`。模型必须支持函数工具和约定 JSON 最终答复。若供应商需要额外字段，使用 JSON 格式的 `MODEL_EXTRA_BODY`；核心请求字段不能覆盖。需要回传内部 reasoning 的特殊协议尚不支持，不能默认兼容所有供应商。

修改配置后重启服务。未配置时界面明确显示“模型待连接”，不生成假查询结果。运行详情保存实际模型输入和工具参数/结果，不保存 Authorization 或 API 密钥。上下文计数当前为标注过的保守估算，不是供应商 tokenizer 实测。

## 数据、规则与代码位置

| 路径 | 内容 |
|---|---|
| `backend/app` | API、模型循环、业务、记忆、保存、上下文与恢复 |
| `backend/migrations` | 按版本执行的 SQL 迁移 |
| `backend/tests` | 实际 PostgreSQL 测试与明确标记的程序模型测试 |
| `frontend/src` | 工作台、结果、记忆管理、运行详情 |
| `docs/财税连续办理助手_PRD.md` / `docs/财税连续办理助手_SPEC.md` | 原始批准文档 |
| `docs/赛题二：财税Harness初赛材料包-v1.1.0` | 原始比赛模拟材料 |
| `.local/postgres/data` | 本机数据库；不进入源码包 |
| `.local/evidence` | 本地测试证据；不默认公开 |

金额使用 Decimal/NUMERIC；发票唯一键是企业与发票号组合。运行数据库角色只具有 `purpose`、`note`、`revision` 的发票更新权限，银行表无写权限。实际修改记录保存在 operations。比赛实例的 Excel 只用于显式初始化；扩展实例通过独立入库角色追加新发票。

## Excel 发票追加（独立扩展实例）

比赛基线继续使用 `.env` 与 `tax_harness`。追加功能使用物理隔离的 `tax_harness_extended`，测试使用 `tax_harness_extended_test`；前端没有把比赛实例改成扩展模式的开关。

```powershell
# 仅首次创建，不覆盖已有配置或数据库
.\.venv\Scripts\python.exe -m app.cli init-extended
# 在另一终端启动，原比赛实例可继续使用 8100
.\scripts\start.ps1 -EnvFile .env.extended -Port 8102
```

已有工程升级须先停止应用并做一致备份，再执行 `.\.venv\Scripts\python.exe -m app.cli init`，应用迁移 003；已有比赛数据不会重新导入。扩展实例已创建时也需要升级，可先设置 `$env:TAX_HARNESS_ENV_FILE='.env.extended'` 后执行同一命令；完成后移除该环境变量。初始化扩展实例时默认只授予 U001 甲、乙企业导入权限，U002 保持只读。

打开 `http://127.0.0.1:8102`，选择企业，点击输入区附件图标。使用 [示例模板](examples/invoice-append.xlsx) 或界面下载模板，按“上传 → 预览 → 明确确认 → 回执”追加；运行记录中的“数据导入”可以核对已有批次。文件上限 5 MiB、单批最多 2000 行、单企业；仅接受标准 `.xlsx` 模板。不会覆盖已有金额、日期、用途、备注或银行记录。错误或冲突默认阻止确认；可明确选择跳过后重新预览。

模型未连接时上述流程仍可操作。配置模型后可携带附件发送消息，由 `prepare_invoice_import` 生成预览，最终入库仍由用户在界面确认。完整文件和行回执在数据库保存，模型只接收受控元信息与分页明细。追加会使旧企业查询/任务快照过期，后续需要重新检查。详细契约见 [patch.md](patch.md)。

`.env.extended` 包含独立数据库凭据，已被 Git 排除。它在首次初始化时复制模型配置；后续修改 `.env` 不会自动同步，需按实际启动实例本地配置并重启。

规则维护需要本机管理员入口：

```powershell
.\.venv\Scripts\python.exe -m app.cli rules list
.\.venv\Scripts\python.exe -m app.cli rules show <rule_version_id>
.\.venv\Scripts\python.exe -m app.cli rules create-version .\rule.json
.\.venv\Scripts\python.exe -m app.cli rules activate <new_id> --replaces <old_id>
.\.venv\Scripts\python.exe -m app.cli rules disable <id>
```

规则正文必须匹配首版可执行模板才能启用；任意新增自然语言规则只能保存草稿。不可绕过 CLI 直接在运行界面启用未经支持的规则。创建格式见 `examples/rule-version.json`。

## 验证

```powershell
.\scripts\test.ps1
```

首次运行全量测试前先执行 `init-extended`。后端测试只使用 `tax_harness_test` 和 `tax_harness_extended_test`，会清空并重建这两个隔离库的模拟数据。不要把 TEST_DATABASE_URL 指向业务库。测试包含真实 PostgreSQL 的权限、金额、事务、来源、版本和恢复断言；ScriptedModel 只验证程序循环。

真实 Chrome 浏览器验收使用独立测试服务：

```powershell
# 终端一：会重置隔离测试库；不要与 pytest 同时执行
.\.venv\Scripts\python.exe backend/tests/ui_server.py
# 终端二：追加功能的隔离测试服务（同样会重置对应测试库）
.\.venv\Scripts\python.exe backend/tests/import_ui_server.py
# 终端三
cd frontend
npm run test:ui
```

测试页面有“程序测试模式”横幅，截图在 `.local/evidence/ui`。默认 Chrome 路径可通过 CHROME_PATH 覆盖。失败保留截图与 Playwright trace。正式模型界面、评测结果不能用这组脚本冒充。68 秒程序测试录屏位于 `.local/evidence/ui/program-walkthrough.webm`。

## 一致备份与离线恢复

先停止本项目应用、评测和测试连接，再执行以下命令；时间点名称自行替换，不覆盖已有备份目录：

```powershell
.\.venv\Scripts\python.exe scripts/recovery.py backup .local/backups/point-001
.\.venv\Scripts\python.exe scripts/recovery.py checkpoint .local/receipts/latest.json
.\.venv\Scripts\python.exe scripts/recovery.py restore .local/backups/point-001 --expect-checkpoint .local/receipts/latest.json
```

备份使用 PostgreSQL 导出的同一事务快照，包含正式数据、来源、修订、删除代次、操作账本、规则与处理水位；记录恢复点、表计数/校验与 dump 哈希。checkpoint 是独立状态核对回执，应另存到可靠位置；它不能代替数据副本，也不能由待恢复的旧备份生成来冒充最新回执。

扩展实例备份/恢复前设置 `$env:TAX_HARNESS_ENV_FILE='D:\tax-harness\.env.extended'`，完成后移除该环境变量。`--test` 根据该配置的 TEST_DATABASE_URL 选择白名单隔离库；不要同时运行对应测试服务或 pytest。

恢复前写维护标记，拒绝其他活动连接。恢复后分别核对备份恢复点和外部回执，只有两者都匹配才开放。旧备份缺少后来的更正/删除时返回 RECOVERY_POINT_BEHIND，并保持 API 与 worker 关闭；需要可靠日志/更新副本补齐后再核对，不应删除维护标记来冒称恢复完成。没有承诺单机磁盘损毁后的最新数据零丢失。隔离恢复演练添加 `--test`。

如果传入的回执本身已经落后，恢复器先与现有数据库核对，返回 RECOVERY_CHECKPOINT_STALE，保留现有数据并保存当前核对回执。维护期间不能从尚未确认的数据重新生成“最新”回执。恢复器输出的 checkpoint_path 可以用于 `scripts/recovery.py resume <checkpoint_path>`；只有当前数据库与维护时锁定的最新回执完全匹配才解除维护。这个命令不能绕过旧备份缺少删除记录的问题。

## 提交边界

`.env`、虚拟环境、node_modules、数据库、便携运行时和私有证据都被 Git 排除。依赖锁为 `backend/requirements.lock.txt` 和 `frontend/package-lock.json`。源码包应通过 `scripts/package_source.py` 生成白名单包，并人工检查设计说明、真实模型记录和 ≤5 分钟视频后提交。

当前原生 Windows 数据库方式已实际运行。Docker 数据库配置已提供，但本机 Docker 引擎不可用，未验证其启动；没有改动或重置全局 Docker。干净机器安装、完整模型评测及比赛正式提交仍需单独验收。
