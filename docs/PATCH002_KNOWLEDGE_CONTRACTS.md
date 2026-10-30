# PATCH-002：知识资料接口与运行契约

实施：2026-10-29；质量批次更新：2026-10-30。对应路线图第3—4步；实测边界见[第一批报告](PATCH002_M4_EXECUTION_REPORT.md)。办理协议仍为 trusted-execution-v1；知识接口为knowledge-v1，选择算法为structural-groups-v1。本批无新增迁移，核心schema需要005和007，向量索引另外需要006。不要用最大迁移编号代替能力就绪检查。

## 1. 数据与模块

| 模块 | 负责内容 |
|---|---|
| `knowledge.py` | 原文件、权限、私有上传、幂等、版本发布和停用 |
| `knowledge_parser.py` / worker | 文本PDF、DOCX、MD、TXT解析、结构定位与分块；有界子进程 |
| `knowledge_jobs.py` | 持久任务、阶段、lease、fencing、索引完整后原子发布 |
| `retrieval_engine.py` | 固定修订的真实BGE-M3向量、jieba分词和BGE重排 |
| `knowledge_retrieval.py` | 查询范围、混合候选、去重/预算、持久引用和输出前重验 |
| `evidence_selection.py` | profile绑定的开发校准策略、结构证据组和完整组预算 |
| `knowledge_admin.py` | 本地企业发布、停用、撤权及RuleBridge；不暴露给聊天写工具 |
| `KnowledgeView` / `KnowledgeCitation` | 上传和状态、检索、版本操作、显示时重验原文、受保护下载 |

原文件作为BYTEA保存在PostgreSQL，记录SHA256、大小、所有者和企业。Document是稳定身份，Version保留每次文件、日期、解析/分块/profile和状态。Blocks保留定位，Chunks及1024维向量是派生索引。普通文件目录不作为知识真值。

`active_version_id`是唯一当前版本入口。ready表示某个候选完整完成索引；候选仍可能因CAS冲突而没有发布。检索必须同时满足文档active、当前指针、版本ready、同profile、权限和生效日期，不能只查ready。

知识与现有任务/偏好分开。明确偏好和当前任务仍精确读取，不改成相似度检索。知识上传不自动创建偏好、任务或发票修改。

## 2. 权限与状态

- HTTP上传仅创建本人私有资料，并绑定一家可读企业；还需要独立的`can_upload_knowledge`。U001样例具有上传能力，U002没有。
- 企业共享必须通过本地维护CLI发布；普通用户不能借HTTP参数升级scope。
- 私有正文、元数据、作业、引用、下载均检查所有者与企业授权。共享资料的非所有者只能读当前有效版本，不能读取未发布版本或草稿文件。
- 日期区间左闭右开。指定月份必须覆盖整月；未指定则按请求接收时点的中国标准日期检索。
- 新版失败保留旧版；旧worker租约过期后不能发布；两个候选竞争时CAS失败者保留ready候选，不能覆盖新版本。
- 私有变化更新用户memory_epoch，共享变化更新company knowledge_epoch。输出前重验，不能继续交付在途已失效片段。

任务阶段：queued → parsing → chunking → embedding → validating → ready。持久作业成功为done；依赖或OCR缺失为blocked，其余解析/索引错误为failed。重试沿用同一job_id并增加尝试次数，不创建另一个文档。停用同时取消queued/running/blocked作业并增加fencing，释放待处理配额；保留文件及ready版本，不让迟到worker发布。cancelled任务不能直接重试，可上传新候选。停用后不能自动重新启用，需明确发布ready版本。

限制：单文件20 MiB、PDF最多200页、解压DOCX最多100 MiB/5000项、解析60秒/512 MiB子进程、每人原文件200 MiB、每小时20次上传、最多10个待处理/阻塞任务。解析子进程屏蔽socket连接，不执行宏或脚本；这不是通用操作系统级文件访问沙箱。

## 3. HTTP

均使用已有登录cookie和same-origin保护。

| 方法 | 路径（前缀 `/api/v1`） | 含义 |
|---|---|---|
| POST | `/knowledge/uploads` | multipart新文档；202仅表示受理 |
| POST | `/knowledge/documents/{id}/versions` | multipart向同一doc追加版本，需expected_document_revision |
| GET | `/knowledge/documents?company_id=C001&offset=0` | 20条分页，返回has_more/next_offset |
| GET | `/knowledge/documents/{id}` | 可访问元数据和版本 |
| GET | `/knowledge/documents/{id}/file?version_id=...` | 核权、hash验证后下载原文件 |
| POST | `/knowledge/documents/{id}/publish` | 私有文档发布指定ready版本，CAS |
| POST | `/knowledge/documents/{id}/disable` | 私有文档停用，CAS |
| GET | `/knowledge/jobs/{id}` | 真实阶段、进度和错误 |
| POST | `/knowledge/jobs/{id}/retry` | 本人失败/阻塞任务重试 |
| GET | `/knowledge/ready` | 验证真实依赖、当前profile和向量表 |
| POST | `/retrieval/search` | 返回持久EvidenceBundle |
| GET | `/evidence/{citation_id}` | 按当前权限和版本重新核验引用 |

上传字段：file、company_id、title、request_id；可选effective_from/effective_to。更新需expected_document_revision。相同请求编号及相同内容复用第一次结果，内容不同返回IDEMPOTENCY_CONFLICT。

查询示例：

```json
{"corpus":"knowledge","company_id":"C001","query":"出差住宿需要谁批准？","document_ids":[],"entities":[]}
```

可选as_of或period，两者互斥。session_id仅归属当前用户。实体是明确F/R/C编号；企业实体必须与company_id一致，企业范围本身不要求正文重复出现企业号。

Bundle包含query_id、profile_id、scope_snapshot、coverage、status、retrieval_mode、candidates的持久trace、evidence、timings和degraded_reason。HTTP返回不暴露全部候选正文，完整排名与分数保留在retrieval_queries。

status为ok/partial/not_found/unavailable。**ok只表示检索和索引范围状态，不表示候选已经回答问题**，answer_support为not_evaluated。没有相关支撑时模型可选择受控knowledge_insufficient，而不能编造答案。hybrid下使用独立开发集校准的选择阈值；全部候选被阈值排除时为not_found，原因BELOW_CALIBRATED_RELEVANCE。这只说明本次范围和选择策略下未找到依据，不能证明事实不存在。

coverage增加selection_version、selection_calibrated、selection_policy_id、selection_cutoff、evidence_tokens、evidence_budget、budget_incomplete。公开组摘要不暴露全部候选ID；完整候选通道、组成员和分数保存在retrieval_queries。策略不存在/不匹配时partial并明确SELECTION_POLICY_NOT_CALIBRATED或SELECTION_POLICY_MISMATCH。依赖故障与空答案分开，降级候选不能冒称通过hybrid校准。

模式为hybrid、lexical_only、hybrid_unranked或unavailable；向量或重排失败不会伪装完整语义检索。只有原文件、没有任何完成profile时返回未就绪，而非空答案。

常见错误：KNOWLEDGE_ACCESS_DENIED、DOCUMENT_UNSUPPORTED、DOCUMENT_TOO_LARGE、DOCUMENT_PARSE_FAILED、OCR_REQUIRED、INDEX_NOT_READY、KNOWLEDGE_VERSION_CONFLICT、KNOWLEDGE_IN_USE、SOURCE_INVALIDATED、STALE_CONTEXT。无权读取私有对象返回404，避免暴露存在性。

## 4. 召回与引用

1. 在SQL里先过滤用户、企业、当前版本、profile和日期，再取候选；精确编号约束同时用于词法与向量通道。
2. 真实jieba分词后的PostgreSQL全文检索最多30条；真实BGE-M3 + pgvector余弦距离最多30条。
3. 单层RRF，k=60，两通道默认等权；取融合前20条，由真实bge-reranker-v2-m3重排。保存各通道排名/分数、RRF与raw logit，不称作正确概率。
4. 通过开发校准阈值的种子扩展为同版本结构组。明确条款的例外、续文、表头共同选取；不跨来源/版本只按相同文本去重。最多6组，连定位/标题按UTF-8字节数/2计入3000估算tokens。整组过预算返回partial及EVIDENCE_BUDGET_INSUFFICIENT，不静默只留主句。
5. 持久引用冻结query、doc/revision、version、chunk、完整excerpt/hash及locator。重启后继续可读；新版发布、停用或撤权后旧引用拒绝重新使用。

组只依据明确标题/编号/例外/表格结构，不识别任意隐含跨章节语义；无明确边界的文档保守扩大组，可能预算拒绝。实体约束保护候选，必要结构上下文可以含其他编号，不能借此更改业务操作目标。实测候选Recall与最终完整证据覆盖分别见报告，不承诺任意表达零遗漏。

新增聊天工具：search_knowledge、read_evidence。知识附件使用独立的knowledge_upload_ids，不复用Excel发票追加接口。read_evidence保持主引用字段，新增related_evidence返回同组其他已核验引用。模型只能用实际入模返回的citation_id选择knowledge_quote；宿主对该引用的完整组逐条核对原文、hash及实际入模状态，生成全部组卡片。成员未入模时要求先read_evidence，不隐式补入新知识。前端卡片再次GET当前引用展示完整原文。历史final_message只保存来源说明，不复制一份永久显示的资料正文。旧M3引用无组元数据时只保持原单条读取，重新检索后才获得新组。

页面每5秒重验已显示引用；失效或请求错误时撤下卡片正文。历史工具来源回读也重验知识引用，不能借search_history复活已停用正文。原文审计记录保留，不宣称从已经被用户看到的设备中抹除内容。

## 5. RuleBridge

资料问答优先区分上传参考资料与正式执行规则。正式财税行为仍经get_rules和受控程序执行，不由相似度决定门槛。

本地维护者将每个适用企业的已发布原文block关联到rule_version。校验当前版本、scope、日期覆盖、原文hash、正文与受控规则模板一致、完整企业覆盖。正文不符或只关联部分企业时拒绝。所有amounts/purpose/bank主题沿用既有规则创建和激活流程。

启用规则引用的文档不能单独换版或停用。先维护规则依赖再操作资料，避免解释来源与执行版本分裂。普通聊天不能创建或激活执行规则。

维护示例（ID和revision从list/show获取）：

```powershell
.\.venv\Scripts\python.exe -m app.cli knowledge list --company C001
.\.venv\Scripts\python.exe -m app.cli knowledge show DOCUMENT_ID
.\.venv\Scripts\python.exe -m app.cli knowledge publish DOCUMENT_ID --version-id VERSION_ID --expected-revision 2 --request-id publish-001
.\.venv\Scripts\python.exe -m app.cli knowledge add-version DOCUMENT_ID --file .\new-policy.txt --expected-revision 3 --request-id update-001
.\.venv\Scripts\python.exe -m app.cli knowledge link-rule RULE_VERSION_ID .\rule-anchors.json --request-id link-001
.\.venv\Scripts\python.exe -m app.cli knowledge disable DOCUMENT_ID --expected-revision 3 --request-id disable-001
```

rule-anchors.json是数组，每个企业一项：`{"knowledge_version_id":"UUID","block_id":"UUID"}`。CLI使用本地admin连接，运行角色没有INSERT规则关联或维护审计的权限。企业候选新版索引完成不会自动替换共享当前版本，需维护者明确publish。

## 6. 已知后续

第4步实现及合成域自动门槛已验证，已知口语误拒1/48和人工语义签收仍待跟进。第5步为历史建立真实向量与日期浏览；第6步补OCR和强制长上下文验证；第7—8步完成组合恢复、部署和参赛交付。当前不支持扫描PDF的OCR，保留原文件并返回blocked，不宣称解析成功。
