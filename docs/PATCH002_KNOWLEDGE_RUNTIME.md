# 知识检索依赖、迁移与验证

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

005创建知识权威关系与权限，006创建vector扩展和向量表，007增加企业维护审计及逐企业规则关联。缺少扩展时006可跳过，核心财税仍可ready；知识索引blocked，安装扩展后再显式init补006。不能仅因007存在就声称向量已就绪。

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
```

启动后登录U001，在“知识资料”上传文本PDF、DOCX、Markdown或TXT。任务显示当前阶段，完成后可查找原文、用于对话、上传新版、下载或停用。U002不能上传本人资料，也不能读U001私有资料。

全局ready中的知识轻量探测会标validation=not_checked；完整检查调用登录后的`GET /api/v1/knowledge/ready`，会校验本地模型manifest及当前profile。首次调用会读取权重hash，不能把轻量探测当成推理验收。

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

## 5. 交付边界

Git只包含源码、锁、迁移、测试与说明，不包含.env、权重、虚拟环境、PG二进制、数据库备份和私人全文。提交比赛时按显式白名单打包，另选合成记录作演示。不要把已完成本地开发、推送、合并main及比赛全量验收混为一个状态。
