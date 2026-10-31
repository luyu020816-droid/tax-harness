# 本地知识能力安装与恢复

本轮支持验证目标为 Windows x64、CPython 3.12、PostgreSQL 16 + pgvector、Node.js。干净目录验证会单独记录结果；Docker 与其他操作系统不能据此宣称实测通过。

## 安装

从源码目录运行（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup-knowledge.ps1
.\.venv\Scripts\python.exe -m app.cli configure
.\.venv\Scripts\python.exe scripts/install_postgres.py
.\.venv\Scripts\python.exe scripts/prepare_pgvector_windows.py --source .tools/pgsql --target .tools/pgsql-knowledge
.\.venv\Scripts\python.exe scripts/native_postgres.py start
.\.venv\Scripts\python.exe -m app.cli init
Set-Location frontend
npm ci
npm run build
Set-Location ..
powershell -ExecutionPolicy Bypass -File scripts/start.ps1 -Port 8105
```

模型接口在本地 `.env` 填写 MODEL_BASE_URL、MODEL_ID、MODEL_API_KEY、MODEL_CONTEXT_LIMIT。BGE 和 Paddle 本地推理不需要 embedding API key；需要实际下载权重及安装依赖，不能仅填配置就声称模型可用。首次安装依赖网络，正式解析时禁网。

主环境使用 `backend/requirements-windows-py312.lock`；OCR 使用独立 `.local/ocr-runtime` 和 `backend/requirements-ocr.lock.txt`，防止升级 OCR 改变已经校准的 BGE profile。Paddle 推理数据在 `backend/ocr-models.lock.json` 固定仓库 revision、文件大小及 SHA256，不执行下载的模型代码。安装后不要在任一环境直接运行无版本约束的升级。

已有完整安装时，不重建数据库、不重新导入 Excel。普通 `start.ps1` 只启动；显式 `app.cli init` 是迁移入口，应先备份。已存在的初始化标记保留已保存用途。不要同时运行使用同一个 `tax_harness_test` 的 pytest 和 evaluate。

## OCR 支持边界

PDF 原文件是权威来源，扫描识别文本是派生内容。支持文本页、扫描页和混合 PDF、规则双栏、普通矩形表格，表格行携带表头及单位，跨页正文保持原页序。引用保留物理页码、PDF 点坐标和包围矩形；详细块级坐标、原始 OCR 文本 hash、解析版本、模型清单 hash、识别分数仍在数据库。繁体转简体与 NFKC 标准化有标记，不进行按答案纠字。

每文件最多20MiB、200物理页，其中OCR最多50页；200dpi渲染，每页最多2000万像素；独立解析进程及其子树合计最多6GiB、600秒。处理前释放本实例缓存的BGE权重，在该实例内串行使用重模型。低于100dpi整页扫描、低置信、缺识别行、合并单元格及不可靠版面均明确失败，保留旧活动版本。评分不是正确率承诺；人仍需核对否定词、金额、单位、专名及例外。

印刷的“第X页 共Y页”与物理页不符会失败。无印刷页码的原件是否少页无法可靠自动证明，须由上传者逐页核对。手写、复杂合并表格、多栏混排不在本轮可靠支持范围，必要时先人工整理为文本/DOCX。没有 OCR 依赖时，文本检索仍可用；扫描件返回 OCR_REQUIRED，不发布空成功。

## 就绪与降级

`/api/v1/ready` 检查核心数据库与模型配置，知识快速状态标明not_checked；完整程序校验 `KnowledgeRetrieval.ready(validate=True)` 检查 BGE profile、选择策略、向量存储，OCR 独立检查可执行文件、固定包版本及模型hash。配置存在不等于接口在线，模型在线性由真实调用另验。OCR缺失不能冒充“识别完成”；embedding缺失不会生成假向量。业务SQL查询不依赖知识向量。底层依赖故障与无答案分别返回 unavailable/partial 和 not_found。

## 备份与恢复

先停止本项目服务、后台作业及连接；备份文件、最新独立回执需存放在不随数据库损坏的存储中：

```powershell
.\.venv\Scripts\python.exe scripts/recovery.py backup .local/backups/review-candidate
.\.venv\Scripts\python.exe scripts/recovery.py checkpoint .local/receipts/review-latest.json
.\.venv\Scripts\python.exe scripts/recovery.py restore .local/backups/review-candidate --expect-checkpoint .local/receipts/review-latest.json
```

备份包含所有public表，原文件bytes和向量有规范化指纹。恢复核对业务、来源与删除、任务偏好、写计划和操作、权限、规则关联、epochs、作业、文件及派生索引。旧备份缺后续删除、撤权或写授权消费时保持维护模式，不能重新生成较旧回执绕过。恢复只能恢复到备份包含的已验证时点，不承诺单机磁盘全损后的零数据丢失。

`verify_system_faults.py` 自动演练时只操作由自身输出目录派生的新库，不恢复用户预览库。`verify_clean_deployment.py` 只占用独立54339/8106，源码白名单包解压后创建两套新环境；允许显式导入且校验公开权重/数据库二进制缓存，不复制开发数据库、`.env`、venv或node_modules。其结果只证明同一机器的干净目录，不代表异机/异OS验收。
