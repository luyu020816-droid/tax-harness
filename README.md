# 财税连续办理助手

按 PRD / SPEC 1.2 实现的本地比赛工程。后端 Python 3.12 + FastAPI，前端 TypeScript + React，数据库 PostgreSQL 16。所有企业、票据与规则都是比赛模拟资料。

业务由同一个模型工具循环办理：查询月度资料、读取适用规则、检查并补充发票用途/备注。模型决定如何理解与追问；程序执行权限、精确计算、保存、核验、幂等和版本校验。生产代码没有按用户关键词选择固定业务答复。

普通聊天保存为独立会话历史，不自动进入知识库。当前会话按 Token 预算组织和压缩上下文，不再固定只取12轮；需要查找过去聊天时，使用独立的日期/中文词法搜索和原文回读。文件上传或手动文字提交才进入知识库。明确保存的任务、偏好与业务账本独立管理，旧统计和旧规则不能替代当前查询。删除会话不连带删除已独立入库的资料和已保存的业务数据，审计记录保留。

## 当前状态

### 评审与演示入口

真实办理使用启动脚本实际端口下的 `/showcase.html`，需要后端、数据库和真实模型配置。`frontend/demo.html` 是标注的交互概念示例，不调用后端、不产生业务回执，也不属于当前生产构建入口。

2026-11-07本轮继续优化：修复组合查询省略企业时的事项范围继承；网络中断改为中文提示，明确服务端处理状态可能未知，并提示保持原输入重发以核对原请求。实现、当前验证结果与未完成项见[本轮优化交付](docs/评审优化交付_20261107.md)。下列各轮历史成绩只代表对应候选，不能合并为当前版本成功率。

2026-11-07保存回执与摘要故障已解耦：无效摘要仍拒绝；完整原文能容纳时继续，否则保留真实失败状态并独立交付已提交回执。完整后端383通过、1跳过；正常保存与保存后摘要故障注入两项真实模型浏览器复验通过，留有桌面和手机截图。见[修复与复验证据](docs/保存回执与摘要故障修复_20261107.md)。

修复前的2026-11-07真实浏览器扩展验收留存55张截图：21个检查点中20通过、1失败。F002已提交并核验后，后续摘要出现SUMMARY_INVALID，整轮未正常交付回执；重复原请求没有二次提交。这是修复前候选的结果，不能被后续重跑覆盖。见[原始截图与完整验收报告](docs/真实浏览器验收报告_20261107.md)。

2026-11-07事项核对（待人工验收）：明确的月度查询、用途检查、规则解释请求增加本轮事项核对，检查对应企业、月份的实际交付，显示已核对、待补充及无法执行。此清单是有限语法的漏办守卫，不保证理解任意组合需求、不授权写入、不代替持久任务。人工操作和限制见[本轮人工验收清单](docs/人工验收清单_20261107.md)。跨轮续办和网络恢复的最新修复见[修复与验收清单](docs/修复与验收清单.md)。本段之后的测试数量均为对应历史版本记录，不代表本轮候选成绩。

独立聊天语义索引已实现。新增异步构建、混合检索、原文跳转和运行诊断。真实 Agent 复验仍有失败，人工验收待完成，演示默认使用日期和词法搜索。见[实施与复现记录](docs/CONVERSATION_SEMANTIC_IMPLEMENTATION.md)。

2026-11-04会话专项已补真实重复、54次三策略和20项权限/失效检查。当前策略开发集18/18；直接词法检索的同义改写0/10仍是明确缺口，60题标签尚待人工确认。见[专项结果与限制](docs/CONVERSATION_ACCEPTANCE_20261104.md)和[人工复核表](docs/CONVERSATION_HUMAN_REVIEW_60.md)。

2026-11-04已完成会话日志、预算投影及独立历史检索改造：后端完整325项回归通过，真实DeepSeek专项5/5，浏览器专项3项通过。主库迁移至14，58张表在隔离库完整恢复并核对原字段一致。当前页面为 `http://127.0.0.1:8105/showcase.html`。下方2026-11-02关闭跨会话聊天检索的描述保留为历史记录，已由本轮独立历史工具替代；旧混合知识入口仍关闭。实现、证据与未完成验收见[会话改造实施记录](docs/CONVERSATION_CONTEXT_IMPLEMENTATION_20261104.md)。

2026-11-02后端已按演示范围加强权限与并发：撤权后隐藏会话内容/标题，支持退出和登录撤销、事务内排队上限、全局活动任务上限、查询/正文资源限制。用户选择暂不接真实认证，`DEPLOYMENT_MODE=production`明确拒绝启动。配置与验证见[后端加强记录](docs/BACKEND_HARDENING_20261102.md)，不宣称真实生产认证或高并发容量已完成。

2026-11-02按用户确认的边界关闭聊天自动索引与跨会话历史召回，摘要按会话隔离；增加文字入库草稿/手动提交，以及会话置顶、归档、删除和完整历史入口。旧索引和报告保留追溯，不能用旧历史RAG成绩代表当前产品。当前实现、迁移和实际验证见[手动知识库与会话管理](docs/MANUAL_KNOWLEDGE_IMPLEMENTATION_20261102.md)。

2026-11-01已使用现有DeepSeek Key执行本批真实模型测试：完整40例业务自动检查40/40、长上下文4项与真实恢复3种边界通过；三策略对比及新增摘要格式反例见[真实模型验证](docs/LIVE_MODEL_VALIDATION_20261101.md)。人工语义验收仍待完成，旧报告保留原基线。

本批按[赛题官网四项加分要求](https://aicxds.yunzhangfang.com/challenge-02.html?v=20261016-6)补充了摘要原话保护、状态变化回归、超时后的原请求恢复入口、恢复前读取回执，以及真实模型策略对比脚本。实现边界和复现命令见[四项加分能力](docs/ADVANCED_FEATURES_20261031.md)，本批证据见[验证记录](docs/ADVANCED_VALIDATION_20261031.md)。下方既有报告保留其历史基线，不能用旧成绩替代本批结果。

2026-10-31代码审查新增：从原始协议反例修复了漏交付已执行结果/回执、漏cursor重回首页，以及删除偏好按文字误伤无关历史，见[基于代码的评委审查](docs/CODE_JUDGE_REVIEW_20261031.md)。已执行结果的交付守卫不等于识别全部子需求；摘要来源标签完整也不等于语义无遗漏。实现提交`cbcaa14`，与此前B/C报告分阶段记录。

后续优先级与可执行验收清单见[剩余 TODO（2026-10-31）](docs/PATCH002_TODO.md)。

第1项的真实长上下文已补充分批压缩、累计来源、最终守卫和实际调用预算验证，见[执行报告](docs/PATCH002_A_EXECUTION_REPORT.md)。原生推理崩溃本轮未复现，根因与日期分页漏游标仍保留为开放问题；不把零故障样本当作已根治。

已实现数据库及工具、任务/偏好管理、上下文预算与摘要、安全保存、租约恢复、规则 CLI、工作台和追溯界面，以及独立扩展实例中的 Excel 发票追加。验证记录见 [实施记录](docs/IMPLEMENTATION.md)、[测试报告](docs/TEST_REPORT.md) 与 [追加功能验证](docs/PATCH001_TEST_REPORT.md)。

整改分支已增加文本知识资料的私有上传、真实混合检索、持久引用、换版与停用，并接入企业维护发布和规则来源校验；本批补充开发/留出评测、校准弃答和完整条款组引用。见[知识接口](docs/PATCH002_KNOWLEDGE_CONTRACTS.md)、[安装与运行](docs/PATCH002_KNOWLEDGE_RUNTIME.md)、[接入报告](docs/PATCH002_M3_EXECUTION_REPORT.md)及[质量报告](docs/PATCH002_M4_EXECUTION_REPORT.md)。整改预览位于`http://127.0.0.1:8105/`，工作区`D:\tax-harness-remediation`。

历史基线（线上聊天召回现已停用）：第二批曾增加历史真实语义召回、无关键词日期分页、长原话续读、来源失效与动态引文，见[历史契约](docs/PATCH002_HISTORY_CONTRACTS.md)及[执行报告](docs/PATCH002_M5_EXECUTION_REPORT.md)。当时后端249项、真实DeepSeek原40例单轮自动检查及历史Agent补验8项通过；这些成绩保留其原候选与策略，不能代替当前手动知识库边界验收。ScriptedModel仅用于明确标记的程序测试。

2026-10-31第2、3项已补齐：独立Paddle扫描件解析、重新校准的知识选择、真实进程故障与56张表完整恢复、两套新环境的干净目录部署。原40例真实模型单轮自动检查40/40；本轮详细边界和证据见[第2、3项报告](docs/PATCH002_BC_EXECUTION_REPORT.md)。新知识留出16个正例、8个无答案及10项边界通过，另有12项真实Agent检查；旧47/48成绩保留，已见反例不冒充新留出。

人工校验使用独立基线 `http://127.0.0.1:8107/`（本地服务运行时），按[人工校验表](docs/PATCH002_HUMAN_VALIDATION.md)逐项签收；[完整知识安装与恢复](docs/PATCH002_DEPLOYMENT.md)包含OCR环境和权重安装。凭据仅在本地`.env`，无需embedding API key。原生推理崩溃根因、历史末页漏cursor、复杂多意图漏答仍开放；OCR真实原件准确性、人工语义签收和参赛交付仍未完成。当前可以进入人工校验，不能宣称最终无缺陷或已经通过比赛验收。

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

本机打开登录页或工作台的「模型设置」，填写 API 地址、模型名称和 API Key，点击「测试并启用」。服务只发送一条模拟工具调用。通过后新办理采用该配置，失败时原配置保留。已通过 `.env` 配置的模型可直接点「测试连接」。页面配置在本次服务运行期间有效，重启后重新读取 `.env`。API Key 不回显，不写入浏览器存储或运行记录。维护入口需要浏览器直接连接本机服务。

本地编辑 `.env`，不要将密钥发到聊天或提交 Git：

```dotenv
MODEL_BASE_URL=https://your-provider.example/v1
MODEL_ID=your-model-id
MODEL_API_KEY=your-local-key
MODEL_CONTEXT_LIMIT=your-provider-documented-token-limit
SESSION_CONTEXT_WINDOW=100000
MODEL_EXTRA_BODY={}
```

接入标准 Chat Completions / function tools 协议。`MODEL_BASE_URL` 是到 `/v1` 的基址；程序追加 `/chat/completions`。模型必须支持函数工具和约定 JSON 最终答复。若供应商需要额外字段，使用 JSON 格式的 `MODEL_EXTRA_BODY`；核心请求字段不能覆盖。需要回传内部 reasoning 的特殊协议尚不支持，不能默认兼容所有供应商。

`SESSION_CONTEXT_WINDOW` 是项目单次请求窗口目标，不限制数据库中的历史消息。输入预算会扣除输出和估算余量；旧环境如显式设置 `INPUT_BUDGET=16000`，该额外上限仍然生效，移除后才按窗口计算。切换到未确认容量的模型时保留保守预算，界面不把推算值当成真实容量。输入框环形是下一次请求的近似估算；输入完整的 `/compact` 可以请求压缩较早对话，原文仍保留。摘要可能遗漏语义，重要内容应回查原文。

修改配置后重启服务。未配置时界面明确显示“模型待连接”，不生成假查询结果。运行详情保存实际模型输入和工具参数/结果，不保存 Authorization 或 API 密钥。上下文计数是 UTF-8 近似估算，不是供应商 tokenizer 实测，也不保证总是高估。

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
