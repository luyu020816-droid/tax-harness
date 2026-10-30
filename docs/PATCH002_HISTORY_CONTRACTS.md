# 历史语义检索与导航契约

2026-10-30；路线图第5步。历史协议`history-passages-v1`，承接已批准SPEC第10.5节。它不代表OCR、真实长上下文或全部恢复演练已完成。

## 1. 真值和派生数据

Source仍为不可覆盖的原话真值，保留用户、角色、发生时间、企业范围、source_seq、revision、hash和recall_state。正式任务与显式偏好精确读取；历史索引不能重建删除偏好、恢复取消任务或提供新写授权。

| 表 | 作用 |
|---|---|
| history_index | 既有同步文本备份索引，供待索引消息的词法回退 |
| history_jobs | 与新Source同事务写入的待处理项；lease、fencing、尝试次数和失败状态 |
| history_passages | Source/revision、字符区间、原文摘录/hash、分词索引、chunker及profile |
| history_embeddings | 派生1024维向量，FK关联passage；不替代Source |
| history_queries | 查询参数、身份/企业/会话/水位/epoch快照、完整候选排名、覆盖和分页 |
| history_evidence | 持久citation、Source修订、字符区间、摘录/hash和本次排名 |

迁移008创建非向量关系并为已有active Source排队；009为可选pgvector索引。核心ready要求005/007/008，向量检索另外需要006/009和真实模型。本批没有改写旧消息、源日期或重新导入Excel。新增表进入原有全表备份与指纹；带非空passage/向量/引用的快照有专门程序回归，组合恢复仍在第7步。

分块为最多600个Unicode字符、重叠最多60字符，并以真实tokenizer复核每块≤1200 tokens，不静默截断。原文片段以字符区间回源。history的excerpt_hash沿用`app.values.digest`；调用方不要自行换成另一种裸字节hash算法。

## 2. 作业和失效

Source保存不调用embedding。后台锁外分块/推理，发布事务检查当前来源状态、revision/hash、权限和租约fencing；全部通过才发布派生块。失败保留原文及原正式状态，作业blocked，显式回填/重试。进程中断后到期任务可接管，迟到执行不能发布。

携带knowledge/history检索结果的工具回包标记skipped，不再次语义索引，避免递归复制原文；它们仍保留审计，日期回读时验证其依赖。其他user/assistant/tool来源保留角色索引。

候选、回读、入模和最终展示均核当前Source状态和权限。更正/删除使旧Source suppressed/deleted，即使旧向量还在，也不能普通召回或读取旧引用。允许保留审计，不承诺物理擦除已经展示过的内容。同一消息多事实仍可能整条保守抑制，其他有效正式事实可通过独立正式证据读取。

本地CPU推理在独立子进程的主线程加载和执行，PyTorch线程数4；宿主串行发送带请求ID的JSON，不向子进程传递数据库或聊天接口凭据。原生异常退出只重试一次纯推理；再次失败明确降级，原文与业务事务不受子进程退出影响。每次响应等待上限120秒，超时终止本实例持有的子进程；应用关闭时清理。真实联调仍观察到torch原生崩溃并成功恢复，不能宣称根因已修复。日志位于被Git排除的`.local/native-inference`。

## 3. 查询、权限与质量状态

`HistoryRetrieval.search/read`为HTTP与工具共用的实现，复用BGE-M3、jieba、真实重排及单层RRF。候选只在本人、当前授权范围、active Source及匹配修订/profile内生成，source_seq必须小于冻结读取水位。

- 不给企业参数只查个人私有来源；company_id查该企业与本人私有来源。
- 明确联合查找可用company_ids，最多4个，与company_id互斥；须逐企业核权，来源范围必须完整包含在请求范围内。不能以单企业查询读取一条同时涉及甲乙的消息。
- 模型工具的联合范围须来自本轮明确企业，或同会话已发布页面的有效游标；不自动遍历全部企业。
- 历史工具回包按真实全部企业记录Source范围，不能误记成个人私有副本。
- 时间为消息发生时间，业务月份另走原业务协议。ISO日期按中国标准时间零点，时间戳须带时区；区间左闭右开。

语义候选：词法30、向量30、单层等权RRF前20、真实重排。历史阈值单独用开发集校准，绑定profile和算法版本，不能复用知识域阈值。当前cutoff=-5.015931129455566，policy_id=`a198d61c0af6804d5e7acea0664b540062af5088bb942c546a76dbdf800568bb`。分数不是正确概率。

模型的query改写不能替换原问题：宿主先检索当前真实用户消息；没有命中才以模型query补查，两次分别持久化，返回alternate_query_ids并重新核验。不降低阈值，不把两次分数再融合成未经校准的概率。精确实体不能被模型改成别的明确编号。原始查询上限2000字符，超出时明确要求缩小。

Bundle有`query_id,corpus,status,retrieval_mode,profile_id,scope_snapshot,coverage,evidence,matches,timings,degraded_reason,pagination`。coverage包括eligible/ready/pending、最早未覆盖消息时间、index_lag_seconds、选择策略和证据预算。lag是最早未覆盖消息距今时间，不是处理单条消息的耗时或队列SLA。

| 状态 | 含义 |
|---|---|
| ok | 当前声明范围/模式可用；不证明语义回答正确 |
| partial | 索引、模式、策略或预算有缺口；已有有效片段仍可引用 |
| not_found | 在本轮范围和选择策略内无可用证据，不证明从未说过 |
| unavailable | 保留的依赖不可用状态；实际可词法回退时用partial/lexical_only |

HTTP保留matches/evidence及content/excerpt兼容别名。`not_found`旧布尔字段仅表示空结果，不能凌驾status。模型输入只发一份正文；partial时去掉这个歧义别名，真实not_found时保留true兼容旧工具。allowed_answer_codes只约束explanation；allow_history_quotes和answer_instruction明确已有支持片段可引用。

## 4. 日期分页和完整阅读

query/entity可省略，此时为日期浏览；须给起止日期，窗口≤31天。直接读Source，独立于向量和索引进度。按source_seq及稳定Source ID倒序，每页最多5条，总摘录预算3000估算tokens。游标冻结身份、范围、会话、epoch和source_seq；新消息不会挤入旧分页，参数变化或记忆更正时拒绝旧游标。

长消息显示最多1200字符并带excerpt_truncated、total_characters、next_char_start。`read_history(citation_id,char_start=next_char_start)`继续读同一原文，每次最多1200字符并生成新的持久引用；可以重构到原消息末尾，不能把前缀当作完整消息。

聊天正式上下文中的history_navigation只包含已完成回复的页面/引用ID、日期角色及下一位置，没有原文或新授权。页面以宿主实际输出的history_page标记定位，未展示的后台查询不能覆盖它。只有用户要求继续时使用，仍须调用工具核验；不会把导航当正文直接引用。跨会话寻找旧消息可用普通检索；活动分页属于原会话，切企业或失效后须重新查找。

模型用`result(view=history_page,result_ref=实际返回)`显示日期整页，宿主逐条生成真实原文卡片并处理末页，避免模型挑漏消息。内部history_page导航标记持久保存，前端不显示技术ID。语义问答仍用history_quote选择支持问题的逐字摘录。

## 5. HTTP与模型工具

```json
{"corpus":"history","company_id":"C001","query":"之前说签章合同放在哪？"}
```

```json
{"corpus":"history","company_ids":["C001","C002"],"date_from":"2026-09-01","date_to":"2026-10-01"}
```

POST `/api/v1/retrieval/search`仍与知识共用，corpus严格区分字段；GET `/api/v1/evidence/{citation_id}`按持久引用类别核验读取。GET `/api/v1/sources/{id}`为原有本人来源审计，不等于普通召回或删除物理擦除。

模型工具：search_history、read_history。工具的会话/原始用户消息/轮次/读取水位由宿主注入，不由模型自报。HTTP调用方自行保留next_cursor；聊天导航仅绑定宿主已完成回复，HTTP后台查询不会抢占聊天当前页。

history_quote仍含source_id、quote，增加citation_id；quote必须属于本轮实际入模content。源完整正文中存在、但本轮未入模的末尾不能偷引。同一citation不同摘录不会互相覆盖。前端以Unicode字符而非UTF-16单元处理位置，显示角色、日期和企业范围，每5秒重验；失效撤下正文。持久final_message只放来源说明，不永久复制一份可能失效的引文。

历史查询轮次只读。当前业务值必须重新查询，助手/工具历史“已保存”只作当时文字；不能借旧Source、旧计划或导航重放修改。是否完成业务修改继续由原操作账本、授权与回读决定。
