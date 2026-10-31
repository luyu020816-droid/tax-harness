# 知识检索依赖、迁移与验证

2026-10-31更新：完整安装（含独立OCR）与恢复以[当前部署说明](PATCH002_DEPLOYMENT.md)为准，知识默认选择策略已切到retrieval-v2；本文保留此前BGE安装与迁移细节。

适用：本次已验证的Windows x64 / Python 3.12 / PostgreSQL 16.15。整改目录`D:\tax-harness-remediation`，数据库54338，预览8105。原54337和朋友8104不受本批变更影响。Linux/Compose组合尚未验收。

## 1. 独立依赖

本批使用目录内独立`.venv`，不修改旧项目虚拟环境。聊天仍使用现有DeepSeek配置；本地embedding与重排不消耗聊天模型key。

新目录安装已经验证的锁定环境：

```powershell
cd D:\tax-harness-remediation
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-windows-py312.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e backend
.\.venv\Scripts\python.exe -m pip check
```

已有本批虚拟环境无需重建。lock包括本批开发和检索依赖；其中torch2.8.0+cpu使用官方CPU源。原基础requirements锁用于旧核心安装，不代表已经安装知识模型。

真实模型由以下脚本下载并核对SHA256；不下载/执行仓库自定义Python代码：

```powershell
.\.venv\Scripts\python.exe scripts/install_retrieval_models.py
```

| 组件 | 固定修订 |
|---|---|
| BAAI/bge-m3 | 5617a9f61b028005a4858fdac845db406aefb181 |
| BAAI/bge-reranker-v2-m3 | 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e |
| jieba | 0.42.1，HMM关闭，词典和停用词hash进入profile |
| pgvector | 0.8.6，conda-forge Windows PG16二进制 |

两份权重合计约4.4 GB，另有tokenizer。文件在`.local/retrieval-models`，不提交Git。实际推理为1024维、CLS池化、L2 float32；安装清单和依赖修订共同组成profile。索引和查询profile不符时不会混用不同向量空间。

本机为16 GB内存，CPU推理线程数4，一次保留一个模型，切换向量/重排模型会重新加载。冷启动和切换有可见延迟；这不是生产吞吐验收。没有GPU部署或并发延迟SLA声明。

## 2. pgvector与迁移

迁移前停止目标服务worker并备份；普通启动不自动导入业务数据。已有D盘本批环境已经完成以下准备，无需重复执行。

```powershell
.\.venv\Scripts\python.exe scripts/recovery.py backup .local/backups/before-knowledge-MYDATE
.\.venv\Scripts\python.exe scripts/prepare_pgvector_windows.py --source .tools/pgsql --target .tools/pgsql-knowledge
.\.venv\Scripts\python.exe scripts/native_postgres.py stop
.\.venv\Scripts\python.exe scripts/native_postgres.py start
.\.venv\Scripts\python.exe -m app.cli init
```

准备脚本复制独立PG16二进制目录后安装扩展，不改原目录，也不自行停/起服务。原始扩展包SHA256：`5730998991a2b7349c4253ed7881f339bd43d005949b655edc616cca8caf21dd`。`native_postgres.py`在准备清单存在时选用新目录，数据目录保持`.local/postgres/data`，端口读取当前项目本地配置。

005创建知识权威关系与权限，006创建vector扩展和向量表，007增加企业维护审计及逐企业规则关联。008增加历史任务、派生块、查询与引用，009增加历史向量表。核心ready要求005/007/008；缺少扩展时006/009可跳过，核心财税仍可ready，语义索引blocked；安装扩展后再显式init补齐。不能仅因权威表已迁移就声称向量已就绪。

新表、BYTEA和向量纳入备份快照。向量按维度和有限float32规范表示生成指纹。本批恢复用例验证文件、向量、版本指针及引用恢复；完整故障组合和异机恢复仍属第7步。

## 3. 启动与配置

```powershell
.\scripts\start.ps1 -Port 8105
```

可选配置（已有合理默认值，无需为本机再次填写）：

```dotenv
KNOWLEDGE_ENABLED=true
KNOWLEDGE_MODEL_DIRECTORY=D:/tax-harness-remediation/.local/retrieval-models
KNOWLEDGE_MAX_FILE_BYTES=20971520
KNOWLEDGE_PARSER_TIMEOUT=60
KNOWLEDGE_LEASE_SECONDS=90
HISTORY_ENABLED=true
HISTORY_SELECTION_POLICY=D:/tax-harness-remediation/evals/history-v1/selection-policy.json
```

默认从仓库内`evals/retrieval-v1/selection-policy.json`加载开发校准策略，无需手填阈值，也不依赖被忽略的本地评测目录。可用`KNOWLEDGE_SELECTION_POLICY`显式指定其他策略文件；文件必须匹配当前真实profile、hybrid模式和structural-groups-v1。更换模型/profile后需独立开发校准并重新验收，不能照搬当前分数。

启动后登录U001，在“知识资料”上传文本PDF、DOCX、Markdown或TXT。任务显示当前阶段，完成后可查找原文、用于对话、上传新版、下载或停用。U002不能上传本人资料，也不能读U001私有资料。

全局ready中的知识轻量探测会标validation=not_checked；完整检查调用登录后的`GET /api/v1/knowledge/ready`，会校验本地模型manifest、当前profile和选择策略，返回selection_calibrated及selection_policy_id。策略缺失或profile不符时知识能力不能通过完整ready；搜索若仍有降级候选会明确partial和未校准，不能冒称与已验收配置等价。首次调用会读取权重hash，不能把轻量探测当成推理验收。ready也不代表检索正确率100%。

文档故障通过知识页面或job接口查看。依赖恢复后由本人重试失败/blocked任务；自动worker只接queued或租约过期的running，不无限重试坏文件。不再使用的资料可停用，相关queued/running/blocked任务会取消并释放待处理配额；原文件仍计入总存储配额。关闭知识开关可停止新增接入，不回退为假向量实现。

## 4. 验证命令

程序回归使用隔离的tax_harness_test，不能与同库评测或测试UI并发执行。

```powershell
cd D:\tax-harness-remediation\backend
..\.venv\Scripts\python.exe -m pytest -q
..\.venv\Scripts\python.exe -m ruff check app tests
cd ..\frontend
npm run build
```

以下是真实组件/模型验证，会创建**合成测试资料**，不会清空正式业务库：

```powershell
cd D:\tax-harness-remediation
.\.venv\Scripts\python.exe scripts/verify_knowledge_runtime.py
.\.venv\Scripts\python.exe scripts/verify_knowledge_http.py
cd frontend
$env:UI_BASE_URL='http://127.0.0.1:8105'
npx playwright test tests/knowledge.spec.ts --reporter=line
```

runtime脚本使用独立tax_harness_knowledge_live；HTTP与浏览器脚本使用8105对应预览库。后者创建/换版/停用自己的合成文档，采用真实检索，聊天步骤调用现有真实DeepSeek；不要把它作为无成本单元测试。不要把其他三个旧UI脚本直接指向真实预览，它们需要各自的固定程序测试服务。

记录在`.local/evidence`及忽略的本地日志中。真实组件三条同义问题只是链路验证，不是独立检索留出成绩。首次PDF取证脚本的排序列错误日志也保留，修复后才有完整PASS证据。

### 冻结检索评测

语料、金标准和策略在`evals/retrieval-v1`，协议见其README。以下命令会新建各自隔离数据库，必须使用新的输出目录；不与同一数据库的其他作业同时运行。CPU真实推理可能耗时较长，`--live-model`还会调用配置的聊天接口。

```powershell
cd D:\tax-harness-remediation
.\.venv\Scripts\python.exe scripts/evaluate_retrieval.py --split development --output .local/evidence/dev-new-run
# 开发策略先锁定，再测留出。当前版本锁定文件已随仓库提交，不覆写它。
.\.venv\Scripts\python.exe scripts/evaluate_retrieval.py --split holdout --policy evals/retrieval-v1/selection-policy.json --output .local/evidence/holdout-replay-new-run --live-model
.\.venv\Scripts\python.exe scripts/verify_retrieval_entities.py --output .local/evidence/entity-new-run
```

重跑已看过的留出只能叫复现，不能作为新一轮调参后的独立测试。`--inference-cache`可复用同profile、完全相同输入的真实向量/重排结果；报告记录来源hash和实际/缓存计数。缓存后耗时不能当生产响应时间。本批结果见[质量报告](PATCH002_M4_EXECUTION_REPORT.md)：候选100%，完整证据47/48，保留1条口语误拒；不是48/48。

`verify_retrieval_agent.py`用于在某次已保留的留出数据库上补测指定问题，需`--evaluation`原输出目录、`--output`新输出目录和一个或多个`--case`。它不初始化或清空该库，原数据库必须仍存在；后加边界文档会改变语料，因此结果与原冻结分数分开记录。

浏览器截图可通过`UI_EVIDENCE_DIRECTORY`指向新目录，避免覆盖前一批图片。未设置时保留旧默认路径。本批验证设为`../.local/evidence/m4-knowledge-ui`。

### 历史检索与回填

历史原话在Source；正式任务和显式偏好仍精确读取。新增Source与history_jobs同事务，后台生成派生索引。迁移008将现有active来源排队，不重写原文、时间和业务数据。009的真实pgvector索引及开发校准策略用于语义查询；日期浏览直接读取Source，无向量也能使用。

```powershell
$env:PYTHONPATH='backend'
.\.venv\Scripts\python.exe scripts/rebuild_history.py --semantic
.\.venv\Scripts\python.exe scripts/evaluate_history.py --split development --output .local/evidence/history-dev-new
.\.venv\Scripts\python.exe scripts/evaluate_history.py --split holdout --policy evals/history-v1/selection-policy.json --output .local/evidence/history-heldout-replay
.\.venv\Scripts\python.exe scripts/verify_history_agent.py --evaluation .local/evidence/history-heldout-replay --output .local/evidence/history-agent-new
```

回填命令幂等重建词法记录并排队缺失、blocked或profile过期的任务；服务worker负责处理，命令返回不代表索引已完成。查询coverage显示当前eligible/ready/pending，partial不能解释为从未说过。策略必须匹配当前真实profile及history-passages-v1；历史与知识选择阈值分别校准。以上评测使用新的隔离数据库/输出目录，重跑已见留出不是新的独立成绩。

BGE推理在受控子进程中串行执行，不需要另一把embedding API key。原生退出仅重试一次纯推理，再失败或响应超时返回DEPENDENCY_UNAVAILABLE；历史查询可明确词法降级，作业blocked并保留Source。每次响应等待120秒，首次模型校验与切换存在可见延迟；不是整个请求的延迟SLA。子进程只获得运行时路径环境，不获得数据库/聊天凭据；日志在`.local/native-inference`，不会把输入正文写到协议错误日志。应用退出会关闭自身推理进程。实际Windows torch崩溃尚未根治，报告记录了隔离后的恢复证据。

浏览器历史专项使用`backend/tests/history_ui_server.py`，仅启动独立8106与tax_harness_history_ui，不清空8105预览库；对应`tests/history.spec.ts`测试旧原话、手机布局和失效撤回。最终本批结果见[报告](PATCH002_M5_EXECUTION_REPORT.md)。

## 5. 交付边界

Git只包含源码、锁、迁移、测试与说明，不包含.env、权重、虚拟环境、PG二进制、数据库备份和私人全文。提交比赛时按显式白名单打包，另选合成记录作演示。不要把已完成本地开发、推送、合并main及比赛全量验收混为一个状态。
