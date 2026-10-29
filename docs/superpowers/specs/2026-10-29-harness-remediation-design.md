# PATCH-002 SPEC：知识库建设与 Harness 可靠性整改

| 项目 | 约定 |
|---|---|
| 文档版本 | 1.0，2026-10-29，北京时间 |
| 状态 | 修改设计与开发契约；未实施，不代表测试已通过 |
| 审查基线 | `feature_adjust_1026`，`203efafead55aa6f119c7588a3c78f857c88932b` |
| 当前 main | 本地 `998d31a`；实施前必须确认并纳入上述分支或等价修复，不得直接按旧 main 开发并覆盖朋友的前端 |
| 文档用途 | 指导编码、接口联调、迁移、缺陷回归、真实模型验收与参赛交付 |
| 优先级 | 正式材料 v1.1.0 ＞ 本 SPEC ＞ 既有 SPEC/PRD 的同主题约定；未改动的既有行为继续有效 |
| 关联材料 | 《01_参赛说明》《03_业务规则》《02_示例数据》；2026-10-28审查报告；2026-10-29要求拆解；2026-10-27 RAG方案 |
| 本次变更范围 | 增加通用知识库与历史语义检索；修复 F01—F09；保留现有任务、偏好、业务工具和发票追加能力 |

“必须”为本补丁交付条件，“初始值”为可配置工程默认值。后文测试目标均为待执行目标，不能复制为已有成绩。P0/P1与里程碑是团队工程安排，不冒充主办方规定。

## 1. 目标、范围与需求追踪

### 1.1 最终行为

用户能在授权企业内上传制度/PDF等资料，查看接入状态，按当前有效资料检索并回到原文；系统能查历史消息，但不把旧消息变成新授权。财税办理继续走同一套 Agent 循环，金额、缺项、权限和保存真值由程序给出。模型不能自行选择没有依据的月份，也不能提交没有真实操作支持的成功回执。

### 1.2 覆盖表

| 编号 | 来源/问题 | 本 SPEC 要求 | 主要验收组 |
|---|---|---|---|
| KB-01 | 已确认的通用知识库扩展 | 文档上传、解析、检索、引用、版本、停用、隔离 | K、R |
| KB-02 | 已确认的历史语义查找扩展 | 长历史混合检索、时间浏览、源角色和删除有效性 | H |
| FIX-01 | F01：缺月份擅自查询 | 可信范围绑定，工具拒绝无来源月份 | S |
| FIX-02 | F02：无保存却称成功 | 最终答复只能引用程序确认的业务结果 | O |
| FIX-03 | F03：否定/引用被错误调用当授权 | 宿主签发且绑定具体修改的写入凭据 | W |
| FIX-04 | F04：正文数量错误 | 关键事实统一由程序计算、呈现 | O |
| FIX-05 | F05：多余追问与错误拒绝理由 | 区分权限、缺信息、资料不足、独立问题与旧任务 | D |
| FIX-06 | F06：规则维护仅覆盖用途 | amounts/purpose/bank 三主题版本管理 | B |
| FIX-07 | F07：真实模型压缩失败 | 统一模型请求配置，真实压缩验收 | C |
| FIX-08 | F08：自动评测漏报 | 状态/事实/交流/防线分开评测，保留失败 | E |
| FIX-09 | F09：提交物过期 | 同步设计说明、关键记录、演示和可运行作品包 | P |

### 1.3 继承的比赛限制

- 官方基线仍为两企业、两用户、40张发票、48条银行记录；只修改用途和备注。
- 用途检查只考2026年9月；8月支持金额查询。企业＋编号联合定位。
- 不猜用途、不根据同额推断付款、不执行记账、付款或申报。
- U001可查改两企业，U002只读甲企业；客户端、模型、文件正文不能改变身份。
- 每用户每企业最多一个未完成任务；明确长期偏好才持久保存，支持查看/更正/删除。
- Excel追加只在既有独立 extended 数据集开启；知识文件上传不是发票追加，不改变官方业务数据。

### 1.4 完整交付范围与非目标

本补丁完整范围包括文本PDF、DOCX、MD、TXT，扫描PDF的OCR，通用知识混合检索，以及历史消息语义查找。文本接入先交付，OCR和历史语义按后续里程碑验收；未完成对应阶段时必须明确标记，不能声称本 SPEC 全部完成。

不新增真实财税能力、互联网爬虫、多Agent、图数据库、外部消息队列、任意自然语言规则自动编程或整站视觉重做。图片语义问答不在本补丁内。不能保证任意自然语言理解、同义召回和所有语义事实绝对正确。

## 2. 技术决策与取舍

| 方案 | 优点 | 代价与结论 |
|---|---|---|
| A：只加RAG和提示词 | 改动小，能较快演示文档检索 | 不修复保存授权、假成功、错误数量；不采用 |
| B：现有单体内增量加入知识模块、范围证据、写入凭据和受控结果呈现 | 复用现有账本、租约、状态和前端；能逐项验证缺陷是否关闭 | 工具与最终答复协议需要迁移；采用 |
| C：所有修改均强制再次点确认，或重写框架 | 确认凭据容易界定，或能统一重构 | 明确指令也重复确认，交互成本高；重写风险大；不作为默认方案 |

选择 B。保留 Python/FastAPI/Pydantic/psycopg/PostgreSQL，前端继续React/TypeScript。PostgreSQL保管正式内容、权限和持久任务；pgvector保管可重建的向量；中文分词后的PostgreSQL全文检索负责词法候选。

本方案的词法排序是PostgreSQL全文检索排序，**不是ES BM25**。不引入另一套Agent框架。模型理解需求、选工具、选证据和处理补充；受控呈现只是保证计算值与操作真值，不用关键词固定回复替代模型决策。

## 3. 必须成立的系统不变量

1. 业务查询范围必须能回到当前输入、已确认任务/上下文或可信界面选择，不能只因为模型给了合法格式就接受。
2. 当前用户、企业权限、数据集类型均由宿主决定。
3. 写入凭据由宿主建立，模型自报`authorized=true`、引用一段原话、持有旧消息或检索结果均不能签发凭据。
4. 一份凭据仅授权一个用户、一个明确业务目标和冻结patch；字段、值、企业、票号变化使凭据失效。
5. 同来源同票仍只有一个冻结意图；重试复用账本，不再次修改。
6. 发票保存成功、核验状态、金额、数量、剩余项由程序结果呈现；没有结果不能渲染成功。
7. 知识得分不能覆盖权限、企业、有效版本、业务规则和当前数据。
8. 资料上传不自动激活执行规则；检索出的旧授权不成为本轮授权。
9. 更新失败保留旧完整版本；停用/撤权对在途回答也生效。
10. 索引、摘要是派生物；不替代任务、偏好、原始文件、正式规则、当前数据及操作账本。
11. 无记录、无证据、索引未就绪、无权限和服务失败必须区分。
12. 日志和提交包不包含模型密钥；业务模型密钥不用于伪装embedding能力。

## 4. 模块与执行流程

### 4.1 模块接口

以下为模块对外接口与职责，不要求为了每个名字新增一个类。内部实现优先集中在少数可测试的模块，避免重复薄封装。

| 模块 | Interface | 隐藏的 Implementation |
|---|---|---|
| RequestEvidence | `prepare_frame(execution)`、`bind_scope(frame,candidate)` | 原文、可信槽位、范围来源、相对日期、冲突与失效 |
| InvoiceWrite | `prepare(execution,candidate)`、`commit(execution,plan_id)` | 直接指令证明、确认凭据、冻结patch、版本、幂等、事务与回读 |
| AnswerRenderer | `validate_and_render(execution,answer_plan)` | 结果引用、状态与说明校验、金额数量格式、回执和引用呈现 |
| Knowledge | `ingest(auth,input)`、`publish(actor,version,revision)`、`disable(actor,doc,revision)` | 文件、版本、权限、解析、索引作业、原子发布 |
| Retrieval | `search(auth,query_plan)`、`read_evidence(auth,citation_id)` | 词法/向量/重排、过滤、版本回读、定位与覆盖状态 |
| RuleManagement | `create_version(actor,data)`、`activate(actor,id,replaces)`、`disable(actor,id)` | 三主题版本、执行模板、日期、冲突、来源关联 |
| ModelRequest | `build(settings,purpose,messages,tools,budget)` | chat/compaction共用参数、供应商extra、保护字段、实际输入与预算 |

现有任务/偏好、Source、Execution、Runtime、ToolExecutor、操作账本与回读继续复用。共享文档单独经Knowledge授权，不能通过放宽所有`read_source`的用户归属解决共享问题。

### 4.2 读业务流程

`用户输入 → 可信RequestFrame → 模型选工具及范围依据 → 宿主绑定范围 → 程序查询当前数据和有效规则 → AnswerPlan → 程序校验呈现 → 保存实际运行记录`

### 4.3 写业务流程

`用户输入 → 模型定位当前记录 → prepare_invoice_write → 宿主核验授权与冻结计划 → 已授权则update_invoice(plan_id)，否则仅展示待确认卡片 → 原子提交 → 回读 → 程序回执及真实剩余项`

### 4.4 知识流程

`上传 → 持久受理 → 解析/分块/建索引 → 验证 → 发布有效指针 → 授权范围内混合召回 → 回读校验与引用 → AnswerPlan → 答复前再次核对版本权限`

## 5. FIX-01：可信范围与RequestFrame

### 5.1 Frame内容

每轮宿主从已有Source、Session、任务和权限构建不可由模型改写的Frame：

- user_id、session_id、turn_id、source_id/revision/hash/seq。
- 可信当前时间及`Asia/Shanghai`，固定为该轮受理时点。
- 界面所选企业、可读写企业及scope epoch。
- 当前任务ID/revision/generation、已确认企业/月份；任务过期状态。
- 经验证的会话范围槽位及其源ID/修订，不将任意历史助手答复当确认。
- 宿主能从明确年月、所选企业和可信任务确定的`available_scope_refs`；未知范围不预造绑定。
- 当前附件的受控元信息；正文不进入授权来源。

Frame写入context manifest，不新增重复存储所有原话的表。

### 5.2 ScopeBinding

模型调用月度业务工具时，`company_id/period`必须绑定`scope_ref`。ScopeBinding由RequestEvidence在首次请求时从候选依据生成，只有宿主存储的绑定ID可用于后续调用。绑定至少记录：user、session、turn、company、period、source refs、来源类型、状态和适用工具集合。

正常情况下模型使用Frame中的available_scope_refs。需要新增组合时调用只读工具`resolve_scope`，输入candidate_company_id、candidate_period和basis_refs；宿主验证后返回binding_id。basis_refs是严格联合类型：current_span含source_id/revision/start/end，task含task_id/revision/generation，confirmed_slot含slot_id/revision，selected_company只补企业；相对日期由current_span定位后由宿主解析。模型不能直接往绑定表写入，也不能通过该工具“确认”没有依据的范围。

月度工具的DTO保留company_id、period并新增必填scope_ref，三者必须与宿主绑定一致。未提供绑定时返回范围错误，而不是自动用模型参数补造一个。已有合法Frame绑定不必额外调用resolve_scope。

允许的范围依据：

| 来源 | 条件 |
|---|---|
| 当前原话的明确日期 | 保存字符区间、原文hash、解析结果；支持2026年9月、2026-09等明确年月 |
| 可信相对日期 | “上个月”等由宿主用Frame时间解析并记录；不让模型自行报当前日期 |
| 已确认任务 | 核对任务身份、企业、revision/generation及当前有效性 |
| 已确认会话槽位 | 关联用户确认的来源；明确更正后旧槽位立即失效 |
| 当前企业选择 | 只能补企业，不能凭企业选择推导月份 |

裸“9月”需要可信年份上下文；数据文件只包含2026年不能自动成为用户选定年份。涉及多个月份时建立多个绑定，分别查询，不用最后一个月份覆盖前一个。

绑定候选若只是模型生成的月份、资料里的例子、被否定的范围或无有效来源的旧助手文字，拒绝。无法可靠区分引用/更正/多候选时返回`NEEDS_SCOPE_CLARIFICATION`；不能通过模型自报“已确认”放行。

查询工具返回绑定ID、实际企业月份和依据，追踪保留至最终AnswerPlan。HTTP显式查询参数代表当前受认证用户的结构化请求，不必伪造聊天来源；仍受权限与范围校验。

明确企业＋票号定位发票时，`find_invoices`不强制月份。不存在月份缺口就不追问月份。

### 5.3 更正与有效期

修改企业、月份、任务或用户权限后，相关ScopeBinding失效。绑定只服务当前turn；下轮从已确认状态重新建立，不直接持有旧绑定跨轮写入。模型调用前与答复提交前校验依赖。

## 6. FIX-03：修改计划与宿主授权凭据

### 6.1 为什么需要两阶段

现有“修改值出现在用户原话里”的检查保留为字段来源校验，但不再当作执行授权。所有发票写入必须先存在宿主持久化的WritePlan；直接调用旧保存函数、伪造字段或持有旧原文不能跳过它。

模型只提出候选；宿主冻结规范化的company、invoice、patch、expected_revision、selection_ref、来源和业务heads，并生成`intent_hash`。

### 6.2 授权的两条合法路径

**路径A：可确定核验的直接指令。** 用户本轮已经明确修改，不重复要求确认。宿主使用版本化、有限且完整匹配的指令校验器核对原文与候选patch，而不是只搜索若干关键词：

- 支持肯定祈使式的明确企业/当前可信企业、明确票号/有效展示序号、用途/备注、修改值及双字段组合。
- 首批支持“把甲企业F002用途填写为X”“甲企业F002用途填X，备注填Y”“第一张用途填X”等验收输入；序号必须对应已展示且仍有效的view。
- 校验器必须输出自己解析的目标与patch，与模型候选逐字段一致；拒绝剩余未解释文本。字段值可有明确引号，不可将整条被引用命令当执行指令。
- 对否定、仅解释、假设、引用、转述旧授权、复杂条件、清空含义不明或解析不完整，**不签发直接授权**。
- 首版“清空用途/备注”必须是明确清空命令或结构化确认，不能以缺值/空白/null自动推断。
- 有限语法不覆盖所有自然语言，这是明确的可用性取舍。模型仍负责总体理解与工具选择；该语法仅做业务写入校验，不能变成三个场景的关键词路由器。

直接指令校验器集中维护，至少按“可选礼貌前缀＋目标＋用途/备注赋值”的完整语句、双字段语句、有效序号语句，以及“先只填F002用途为X，其余还没确认”这类明确部分办理语句测试。先做Unicode NFC与首尾空白规范化，不改写实质字段值；值内分隔符需要明确引号，无法界定则走确认。否定/引述检查针对命令外层，不因字段值恰好含“不要”等词误判用户在禁止执行。测试必须同时覆盖可接受语法及其最小否定/引用变体，禁止把完整匹配退化成任意位置搜索动词。

**路径B：结构化确认。** 合理但不能被路径A充分核验的自然语言，返回冻结计划卡片，由本人在界面确认，不能先保存：

- 卡片展示企业、发票、原值、新值、字段、是否清空、仍未处理的目标。
- 确认请求只提交plan_id、expected_plan_revision、intent_hash、request_id；服务端从计划读取真实patch，不能从客户端重新接收可替换的修改值。
- 该控制入口校验现有登录身份、同源与CSRF防护；请求不能包含另一个user_id或新的patch。过期登录、企业权限撤销与切换上下文均重验，不把浏览器按钮本身当权限。
- 只解释/明确不执行的请求标为`prohibited`，不展示诱导用户保存的确认卡片。复杂意图不明时先澄清，不自动生成无关计划。
- 计划默认10分钟过期。确认时重新核权、检查业务revision与依赖；有变化则拒绝旧确认并展示新状态，不静默改用新值。
- 控制入口创建宿主`user_action`来源与新的可恢复控制轮次；确认轮次直接执行冻结计划并生成程序回执，不让模型再次自由选择目标。
- 非网页入口必须提供等价受认证的结构化确认入口；不是在回复正文中让模型伪造“用户已确认”。

路径A不能解析时可用路径B，不得回退到旧子串检查直接提交。为了演示顺滑而扩大自动授权语法，必须补相应正反例并升级校验器版本。

### 6.3 WritePlan状态与凭据

`drafted → authorized | needs_confirmation | prohibited`；随后可进入`consumed/revoked/expired`。

authorization_kind仅为`direct_command`或`user_confirmation`，均由宿主写入。计划存：

`plan_id, user_id, session_id, origin_turn/source/revision, authorized_source_id, company_id, invoice_id, patch, before_values, expected_invoice_revision, selection_ref, observed_heads, revision, intent_hash, status, authorization_kind, verifier_version, expires_at, operation_id`。

数据库约束：仅用途/备注；patch非空；同用户同来源同票同一冻结意图；来源、凭据、目标和用户不可分拆替换。模型提供的plan_id仍须核验归属、状态、修订及hash，UUID本身不是权限。

origin_source_id始终是原始用户请求；authorized_source_id在直接指令路径为该原话，在确认路径为新的宿主控制来源。operation.source_id指向实际授权执行来源，另经authorization_plan_id追溯origin_source_id。intent_key按origin计算，防止确认重试生成第二次写意图。控制轮次使用既有持久work_items与租约，但由turn.request_kind分派到受控执行，不将控制内容当新聊天命令。

### 6.4 提交事务与幂等

同一事务按既有锁序核验Execution/用户 → 企业head → 计划 → 发票 → operation；检查当前权限、source有效性、plan状态/hash、发票revision与依赖。

- intent_key稳定绑定`user + origin_source + company + invoice`；确认建立新来源不能生成第二份独立写意图。
- 计划冻结的patch写入既有operations账本，不另建第二份业务真值。
- 同一事务提交发票、operations、计划consumed及任务变更；失败全部回滚。
- consumed计划重试只查询既有operation并回读；不再次UPDATE。
- 提交后仍调用既有verify，区分verified、pending、changed_since_commit等状态。
- 直接写函数缺有效plan即`WRITE_AUTHORIZATION_REQUIRED`；内部代码、HTTP和模型工具不得保留旁路。

旧任务、偏好和检索来源不签发发票授权。任务/偏好显式管理继续走本轮来源与revision校验；知识正文不能触发它们。检查结果自动刷新任务属于程序派生状态，不需要用户为每次进度更新重复确认。

### 6.5 安全和语义边界

宿主凭据证明的是“该具体patch满足受控直接语法或本人确认”，不是证明任意自然语言都被完美理解。未识别输入进入澄清/确认。不得用第二个模型随意判一个布尔值，再宣称授权已被确定性证明。

## 7. FIX-02/04：AnswerPlan与程序呈现

### 7.1 协议变化

用严格的`AnswerPlan`替代模型可自由提交的`FinalReply.message`。模型选择展示哪些实际结果、哪些依据、是否需要澄清；业务事实和成功状态由程序生成。旧客户端仍读取`turn.final_message/final_cards`，其内容改为宿主渲染结果。

模型输出示意：

```json
{
  "status": "completed",
  "blocks": [
    {"type": "result", "result_ref": "本轮真实结果引用", "view": "monthly_summary"},
    {"type": "explanation", "code": "independent_totals"}
  ],
  "clarification": null,
  "resume_turn_id": null
}
```

示意字符串不是可接受的固定ID；实现使用宿主实际签发的引用。schema禁止额外`message`、任意业务数字、伪造operation状态和客户端式授权字段。

### 7.2 Block类型与校验

| 类型 | 可选数据 | 程序约束 |
|---|---|---|
| result | 实际tool result/source引用、允许视图及字段集合 | 来自本轮已实际入模且仍有效的结果；金额、数量、行顺序不由模型填写 |
| operation | operation_id | 归属当前用户/本轮授权意图；根据账本与回读状态生成回执 |
| preference_receipt | 已提交memory_change_op引用 | 延续本轮真实偏好提交校验，不让新协议削弱已有防线 |
| knowledge_quote | citation_id与已取出的原文区间 | 校验逐字摘录、文档版本/权限；明确以资料引文展示，不当作系统办理状态 |
| history_quote | citation_id | 保留user/assistant角色及日期，不把助手历史声明当业务真值 |
| explanation | 受控code＋已验证的结果/规则引用 | code的前提由程序检查；不接受模型自行声明“已保存”等自由句子 |
| confirmation | plan_id | 仅needs_confirmation且本人有权，展示冻结卡片 |

P0知识回答采用“简短引导＋相关原文引文＋来源定位”的可追溯形式，模型选择证据与顺序；不开放可混入业务成功声明的任意正文。跨文档差异按引用并列展示，未能支持结论时明确说明不足。

result视图首批覆盖monthly_summary、purpose_check、invoices、rules、task、preferences、invoice_import_batch，均由对应工具的实际结果类型登记。result_ref使用真实tool-result Source UUID；操作回执另用operation block，不能通过result通道注入任意成功文本。受控引导文本由程序产生；knowledge_quote/history_quote中的引文始终显示为原文资料，不渲染成宿主状态。

这是对旧RAG方案自由生成答复部分的明确收紧：自由改写摘要可作为后续增强，但不能成为当前业务状态输出旁路，也不能冒充已经由程序证明语义正确。

### 7.3 explanation与状态

首批受控说明覆盖：独立金额、无记录、无修改请求、全部用途必填、门槛规则、不支持的用途月份、只读、无访问权限、目标不存在、无法判断付款、知识无证据/冲突/不可用、待核验、当前值后来变化、没有未完成任务。

模型选择说明code而非关键词触发整段业务流程。说明前提必须由有效工具、Frame权限或本轮明确状态支持；例如有访问权限时不得渲染access_denied，没有查记录时不得渲染no_records。

- `completed`表示本轮已答复，可以是明确拒绝，不等于业务保存成功。
- `waiting_user`必须有当前问题实际缺失字段或待确认计划。
- 已有企业＋票号时不能为定位发票强制补月份。
- 独立知识/历史问题答完，不因旧任务仍缺月份就强制waiting_user。
- 历史待澄清仅作为候选；只有本轮实际接续才设置resume_turn_id。

### 7.4 事实与回执规则

- 金额由Decimal/NUMERIC生成两位小数；缺项数、总数、已填数、剩余项来自程序结果。
- 不让模型在正文重新列一遍卡片完整清单或改变编号顺序。
- `verified`才允许“已保存并核验”；`no_change`显示当前已是该值；pending不能显示已核验；changed_since_commit显示本次曾提交、当前已变化。
- 未出现实际operation不得生成operation block；本轮只查询不得凭历史operation生成新保存成功。
- 所有关键引用在最终提交事务中重验；失效时重新查询/检索，不为旧内容换一个新版本号。

## 8. 知识库接入、权限与版本

### 8.1 知识分层

| 信息 | 权威存储/读取 |
|---|---|
| 任务与明确偏好 | 既有关系表精确读取，不由向量命中决定当前值 |
| 当前发票与银行金额 | 既有业务表及程序查询 |
| 生效业务规则 | rule_versions＋scope/date/status及受控执行profile |
| 企业制度/一般资料 | 新知识文件、解析正文、版本与授权检索 |
| 历史对话 | 既有sources，索引帮助定位，回读核权和修订 |

### 8.2 访问控制

文档只允许`user_private`或`company`，必须绑定一个企业；无全局共享池。私有文档仅拥有者且仍有企业读取权限时可读。企业文档按企业读取授权，发布由本地维护CLI完成；聊天工具不授予共享发布或规则维护能力。

新增`can_upload_knowledge`，必须同时有can_read。初始化建议U001对两企业可上传，U002默认不可上传；这不改变其发票修改权限。拥有者只能更新/停用本人私有文档，不能通过修改scope将其变成企业共享；共享发布为显式维护动作。

新文件默认私有。查询候选生成前过滤身份、企业、scope、有效版本和时间，重排后及原文读取时再次核验。共享文件不能复用现有仅属个人的Source读取接口放宽所有权。

### 8.3 原文件和状态

原文件以受限BYTEA保存，连同文件名、MIME、字节数、sha256、拥有者、企业和request_id持久化。不同用户相同hash不能泄露“别人已有此资料”。

受理事务一次建立file/document/version/job，返回202只表示已受理。状态：

`queued → parsing → chunking → embedding → validating → ready`；异常为`failed/blocked/cancelled`。只有ready并被active_version_id选中的版本才是当前可检索版本。

初始上限：20MiB/文件，文本PDF200页，OCR50页，2000chunks/版本，200MiB原文件/用户，20新上传/小时，10未完成作业/用户。解析60秒、OCR600秒；单页渲染最多2000万像素。这些是可配置资源上限，不是实测性能。

请求体在读取过程就限制大小；检查真实格式，不只看扩展名。解析子进程不得访问任意外链或执行宏/脚本，超时终止整个子进程树。缺少依赖、损坏、空正文、低质量OCR均不发布空成功。

### 8.4 定位与分块

- PDF保留1起始物理页序及可获得的bbox，单位PDF points；扫描页保留OCR置信与解析器版本。
- DOCX保留段落/表格/单元格定位，不伪造稳定页码；MD/TXT保留行号和字符区间。
- 先解析标题、正文、列表、表格、页脚等blocks，再按结构切分。segment目标300—500tokens；检索chunk目标600—900，硬上限1200；必要重叠不超过80。
- embedding输入长度按实际tokenizer计算，标题、元信息和前缀计入；禁止静默截断。
- 短完整规则尽量整条保留；否定、例外、阈值、表头、单位、脚注与相关条款一起成为证据。超长表格行结构化展开或明确报错，不能截成丢单位的片段。
- OCR用本地PaddleOCR适配器；多栏顺序、表格和缺页分别验收。页数一致不能证明语义完整。

### 8.5 更新、发布与停用

采用单篇新版本全量重建。旧版本继续可读，候选完整验证后，以`expected_document_revision`执行CAS发布。迟到作业不能覆盖后来已发布的新版本。

私有首次ready允许基于预期revision自动发布；企业共享始终由CLI显式发布。发布原子更新active_version、document revision、旧版状态及scope epoch。新版本失败不先删旧索引。

停用后普通检索与新答复不能引用。已关联生效执行规则的文档禁止直接停用/删除，返回`KNOWLEDGE_IN_USE`，必须明确替换/停用关联规则或在一次维护事务中联动处理。默认软删除与审计保留，不声称物理抹除历史。

## 9. 数据模型与迁移契约

不复制现有发票、任务、偏好和operations为新真值。新增或扩展关系如下；所有外键、归属、唯一性和状态转换须在迁移中约束，不能只靠前端。

| 关系 | 核心字段/约束 |
|---|---|
| scope_bindings | binding_id、user/session/turn、company、period、basis_json、状态、依赖hash；绑定当前轮且可撤销 |
| write_plans | §6.3字段；唯一用户/原始source/企业/票号的冻结意图；authorization不可由模型直接写 |
| turns扩展 | request_kind=chat/write_confirmation，control_ref；仅宿主确认入口可建立控制轮次 |
| operations扩展 | authorization_plan_id；回执能追溯原始用户请求及确认来源；保留既有intent_key唯一约束 |
| knowledge_files | file_id、owner/company、bytes、MIME、size、sha256、request_id/hash；同用户请求幂等 |
| knowledge_documents | doc_id、owner/company/scope、title、revision、status、active_version_id |
| knowledge_versions | version_id/doc/file、edition、effective_from/effective_to、expected_doc_revision、解析/索引状态、内容hash、计数、parser/chunker/profile版本、错误 |
| knowledge_blocks | block_id/version、ordinal、类型、heading_path、正文/hash、locator |
| knowledge_chunks | chunk_id/version、ordinal、block_refs、检索正文、tsvector、token_count/hash；唯一version/ordinal/chunker |
| history_passages | passage_id、source/revision、字符区间、hash、tsvector、chunker；Source仍为权威 |
| retrieval_profiles | profile_id、embedding/tokenizer/reranker修订、维度、归一化、长度、词典、状态；不可混空间 |
| retrieval_embeddings | profile/target_kind/target_id/revision/input_hash、vector(1024)；唯一profile/target/revision |
| retrieval_jobs | job_id、业务唯一键、target/version/profile、state、attempts、lease、fencing、progress/error |
| retrieval_queries | query_id、user/session/turn、原始/改写问题、corpus、范围版本、profile、候选摘要、覆盖和耗时 |
| retrieval_evidence | citation_id/query、源类型/ID/版本、locator、摘录/hash、角色、排名；读取仍回源核权 |
| rule_knowledge_refs | rule_version_id、knowledge_version_id、locator、excerpt_hash |
| company_heads扩展 | knowledge_epoch；与data_revision、rules_epoch分开 |
| user_company_grants扩展 | can_upload_knowledge，要求can_read；不等于can_update |
| rule_versions扩展 | 保留真实topic；新增execution_profile与受控版本标识，不把文本变更当程序变更 |

知识索引作业使用独立retrieval_jobs，因为无聊天turn的上传不能硬塞进既有work_items身份链。

建议顺序为004请求证据与输出协议、005知识权威关系与三主题规则、006检索派生关系/pgvector。实际编号以实施时已存在迁移为准，禁止覆盖已执行迁移。每一阶段声明可兼容schema范围；`db.ready()`不能继续写死version==3。

迁移不得重导Excel或重置当前用途。旧operation保留审计读取；不能自动把旧Source转成新授权凭据。旧最终答复可显示为历史记录，新轮次只用新协议。迁移前已有在途模型执行须受控停止/失效，不让旧协议在新版本启用后提交。

## 10. 检索、历史召回与证据契约

### 10.1 索引初始实现

- 向量：BGE-M3 dense，1024维；本地适配器按需加载，部署记录模型实际revision、tokenizer、归一化方式、运行依赖与输入上限。
- 词法：应用侧中文分词，固定词典版本，生成PostgreSQL `simple` tsvector，GIN过滤与`ts_rank_cd`排序。业务编号、企业名及规则编号单独保留。
- 重排：bge-reranker-v2-m3适配器；记录实际修订。raw logit或sigmoid值都不是已校准正确概率。
- 初始小语料使用权限过滤后的精确向量距离。HNSW仅在实际规模和过滤后召回测试证明需要时增加。
- 模型变更建立新profile并完整重建后切换；即使维度相同，也不能混合不同模型向量。不同维度须迁移，不靠补零兼容。
- embedding服务实际tokenizer与max_length必须联调确认。模型卡上限不能代替服务端不截断的实测。

DeepSeek聊天接口不被假定提供embedding。本地embedding、重排与OCR的可用性独立于聊天模型；未安装时返回blocked/unavailable，不能拿假向量填库后宣称语义检索完成。

### 10.2 QueryPlan

输入至少包括corpus=`knowledge/history`、query、企业绑定、实体、检索模式及可选日期。区分：消息发生时间、文档生效时间、业务月份。不得把“去年说过”改成“去年业务月份”。

文档版本增加effective_from/effective_to（后者排他）元数据；文档资料生效时间默认以本轮可信日期查询，用户指定日期则依明确来源。财税执行规则仍经RuleManagement按业务月份选择，不用一般文档默认日期替代。仅覆盖部分业务月份的文档不能被宣称适用于整月。

只在指代、上下文省略确有需要时改写问题；保留原问题。改写不能增删明确企业、票号、金额、否定或授权语义。多个候选不靠向量分数替用户决定。每次检索仍保留原始问题通道，不能只检索改写句。

query最多2000字符，词项最多32个，均参数化传入SQL；不拼接tsquery。精确票号/规则编号为已确认实体时是候选约束，不能被语义高分改成相似编号。

### 10.3 混合召回

1. 冻结身份、企业、有效版本/日期、memory_epoch、knowledge_epoch、profile和历史读取时点。
2. 在权限与有效版本集合内生成向量30、词法30候选；按实际实体增加精确候选。
3. 以逻辑来源/版本去重，单层weighted RRF融合：`score=Σ weight/(60+rank)`，向量与词法初始等权。
4. 融合前20进入重排，保留原通道名次与分数用于评测，不把不同原始分数直接相加。
5. 回源校验hash、角色、修订、权限、状态；不合格候选排除。
6. 按需要扩展同条款的否定、例外、表头、单位、脚注；最多6组证据，默认总预算3000个当前保守计数器估算tokens，定位和扩展也计入。
7. 必要完整证据放不下时返回预算不足/缩小问题，不截断关键限定词以凑top-k。

不默认使用两层RRF，不写死未经开发集校准的0.15或相对分数比例阈值。降级为lexical_only、hybrid_unranked时必须在返回和trace记录；效果是否可用由该模式留出评测决定。

### 10.4 EvidenceBundle与无答案状态

返回`query_id, corpus, status, retrieval_mode, profile_id, scope/epoch snapshot, raw/rewritten query, coverage, evidence[], timings, degraded_reason, pagination`。

每条证据持久化citation_id、原始类型/ID/修订、版本、生效/启停状态、角色、locator、摘录和hash。HTTP检索和模型工具检索都写同一查询/证据记录，不能只存在内存临时对象。

状态至少区分：

| 状态 | 含义 |
|---|---|
| ok | 在已声明范围内找到可用证据 |
| partial | 只覆盖部分问题或部分语料，不能推出整体不存在 |
| not_found | 在明确的检索范围与模式下未找到足够证据 |
| conflict | 找到相互矛盾的依据；并列展示，不擅自激活某个规则 |
| unavailable | 依赖或服务失败，不等于无资料 |

coverage包含eligible/ready数量、待索引缺口和index lag。语义索引未补齐时，普通词法命中仍可读，但不能将向量无命中解释为从未说过。

### 10.5 历史查找

- 正式任务与偏好始终精确读取，历史语义仅帮助找原文。
- 候选须`source.user_id=当前用户`、企业允许、recall_state有效、源修订匹配；source_seq不能超过当前输入时点。
- 省略企业时只查个人私有历史，不自动混查所有企业；跨企业查找须用户明确指定并逐范围核权。
- 对至少30天前消息、跨会话同义表达、明确实体、角色和更正删除分别测试。
- 新增纯日期浏览，不强制编造query/entity；默认单页5条、单次日期窗口≤31天，按来源顺序分页，直接读权威Source。
- 结果包含user/assistant/tool角色；助手历史“已保存”仅是历史文字，当前状态必须重查。
- 更正删除同步改变正式状态、recall_state、epoch和摘要有效性；向量清理可异步，但陈旧向量回读不得复活旧记忆。
- 同一原话包含多个事实时，当前抑制可能保守影响整条普通召回；保留仍有效正式事实的独立证据访问，不声称已实现逐语义物理擦除。

## 11. FIX-06：三主题规则管理与知识衔接

### 11.1 管理职责

保留本地CLI作为维护入口，补齐`amounts/purpose/bank`导入、list/show、创建新版本、activate/replaces、disable。不能再在INSERT中写死purpose。模型聊天工具只有查规则能力，无共享发布或激活规则能力。

每个版本具有规则编号、名称、业务版本、topic、适用企业、生效区间、status、full_text、执行profile/参数及来源。新内容建新版本，不原地改生效文本。旧版仍可审计，但不能作为当前依据。

### 11.2 受控执行profile

| topic/profile | 可维护范围 | 不支持的变化 |
|---|---|---|
| amounts / amount_stats_v1 | 名称、版本、企业、生效区间和明确来源；规范条款由固定profile生成 | 取消十进制、把收入抵支出、用旧Excel数值代替查询等必须拒绝激活 |
| bank / bank_readonly_v1 | 说明版本、范围、日期和来源；程序只读及独立收支含义保持 | 修改银行记录、自动发票付款匹配必须拒绝 |
| purpose / purpose_all_v1 | 全部必填，生成完整条件正文 | 任意代码或未支持条件保持草稿 |
| purpose / purpose_gte_v1 | 非负两位小数门槛、gte、空白与备注语义 | 复杂表达式或其他比较符需明确新增profile后才能执行 |

规范性条件正文由profile与结构化参数生成，说明性补充与原始文件另外保留、标识为非执行条件。不能对任意自由文本自动“理解后批准”。迁移为原始五项规则建立等价profile映射，保留其原始全文与来源，核对全部官方边界结果不变。

R001/R004可创建、启用新知识版本，但本补丁不允许通过文档把比赛禁止的计算或写权限改掉。此限制是执行profile的边界，不是省略规则版本管理。

### 11.3 日期、替换与冲突

统一生效区间为`[from,to)`；月度规则适用须覆盖整个查询月。当前执行profile限月首边界；一般文档元数据可到日。

启用事务锁定所有受影响企业heads，校验topic、企业和生效区间冲突。`replaces`必须同主题且作用域集合一致；跨企业拆分/合并必须显式给出完整替换集合并在同事务处理，不可因为只维护甲企业而悄悄停掉乙企业共享规则。所有受影响企业rules_epoch递增。

### 11.4 文档到规则的桥接

上传制度只成为参考资料。维护者可将某冻结知识版本及定位关联为规则来源，经受控profile、正文、范围、日期和冲突检查后激活。生效规则回答与check_purposes读取同一正式版本，不受RAG排名影响。

有效规则依赖的知识文档不能单独停用；维护事务明确替换或停用规则后才允许。提供维护预览与操作审计，不增加多人审批系统。

## 12. FIX-07：上下文、模型请求与压缩

### 12.1 统一请求构造

chat和compaction通过同一ModelRequest模块处理MODEL_EXTRA_BODY、模型ID、思考模式、响应格式、保护字段、超时和预算；compaction只省略工具，不丢失供应商必要参数。

保留现有DeepSeek非思考模式配置。用户密钥只从本地配置加载，不复制入payload、manifest或异常输出。配置中的model_id只是请求模型名，报告区分供应商实际返回的model版本（若提供），不凭别名声称固定旧权重。

额外参数禁止覆盖messages/tools/model/stream/max_tokens和受控响应协议。模型不支持所需工具/JSON协议时明确失败，不能切换固定业务回复冒充已接通。

### 12.2 上下文组成

必须保留：系统约束、Frame、当前用户原话、有效任务/偏好、当前授权/计划状态、当前必要规则与完整闭合工具组。检索证据按独立预算进入，当前业务数据仍需实际查询。

manifest新增`request_frame/scope_refs/write_plan_refs/knowledge_dependencies/retrieval_refs/knowledge_heads/profile_id/retrieval_mode`。仅记录真正进入本次payload的证据引用；被裁剪或从未发送的候选不能用于最终引用。

按预算裁剪旧对话、可选摘要、旧完整工具组；不能切开tool call/result配对。必要状态放不下则返回明确容量不足。当前token计数标识为保守估算，不冒充供应商精确tokenizer。

### 12.3 压缩发布

保留原文，摘要只作导航。来源处理状态、source revision、memory_epoch校验后生成；生成期间变化则候选废弃。不能仅以covered_sources数组齐全作为语义无损证明。

实际触发一次真实DeepSeek压缩，验证摘要发布、协议正确及企业、月份、否定、更正、剩余事项。摘要失败应保留原文与正式状态，trace标记降级；无足够证据时回原文/澄清，不把缺省当成功。

### 12.4 答复前守卫

既有guard、租约、fencing、memory/data/rules heads继续生效；增加知识head和有效版本。模型请求前和最终提交前分别核验。若企业文档在另一个用户请求中停用，持有旧片段的本轮也必须失效。

## 13. HTTP、模型工具与前端契约

### 13.1 新增或调整的HTTP入口

| 入口 | 约定 |
|---|---|
| POST `/api/v1/knowledge/uploads` | multipart file、company_id、title、request_id；202返回doc/version/job；默认本人私有 |
| POST `/api/v1/knowledge/documents/{id}/versions` | multipart新文件＋expected_document_revision；不另外创建无关doc |
| GET `/api/v1/knowledge/documents` | 分页列表，本人/授权企业范围；状态与active版本清楚 |
| GET `/api/v1/knowledge/documents/{id}` | 元信息、版本、定位、当前状态；核权 |
| GET `/api/v1/knowledge/jobs/{id}` | 真实阶段、进度、失败与可重试性 |
| POST `/api/v1/knowledge/jobs/{id}/retry` | 本人权限及request_id；复用作业业务身份 |
| POST `/api/v1/knowledge/documents/{id}/publish` | 仅本人私有ready版本，expected revision；不能变成company scope |
| POST `/api/v1/knowledge/documents/{id}/disable` | 本人私有；有关联生效规则则拒绝 |
| POST `/api/v1/retrieval/search` | 受认证的QueryPlan；返回持久EvidenceBundle |
| GET `/api/v1/evidence/{citation_id}` | 重验权限/版本/源hash；返回可定位原文 |
| GET `/api/v1/knowledge/ready` | parser/OCR/vector/embedding/reranker/profile状态，无凭据 |
| GET `/api/v1/write-plans/{id}` | 本人冻结计划/有效状态；不暴露其他用户计划是否存在 |
| POST `/api/v1/write-plans/{id}/confirm` | 本人结构化确认，expected revision/hash/request_id；生成可恢复控制轮次 |
| POST `/api/v1/write-plans/{id}/cancel` | 仅撤销计划，不撤销已提交业务；已consumed则返回真实回执 |

修改现有`turns`输入时新增独立`knowledge_upload_ids`，不复用当前发票专用`upload_ids`。一轮有知识附件不等于授权修改任何发票；正文经检索工具进入，附件只注入元信息。

### 13.2 模型工具变更

- 新增`search_knowledge`、`read_evidence`；增强`search_history`的语义/日期模式。
- 新增只读`resolve_scope`；月度业务工具增加必填scope_ref，按§5.2签发或选用真实绑定，缺失来源给结构化纠错。
- 新增`prepare_invoice_write`，参数为候选目标/patch/revision/来源/selection_ref，返回宿主计划。
- `update_invoice`改为只收plan_id，真实patch从宿主计划读取。保留工具名有利于迁移，但旧参数组合必须拒绝，不静默兼容旁路。
- `get_rules`保持精确规则选择；`manage_preference`继续真实账本校验；无模型用的共享发布、规则activate或伪造确认工具。
- 最终答复严格采用AnswerPlan；格式修复沿用最多2次，仍受总步数、工具数、超时限制。不能为重试而自动增加无限预算。

### 13.3 UI改动范围

保持朋友已有暖色主题、狐狸标识、导航布局和响应式样式。只增加必要交互：

1. “知识资料”入口：本人/企业资料列表，上传、新版本、真实接入状态、失败重试、本人私有停用；企业共享维护仍CLI。
2. 知识附件与发票附件显式区分，不能让用户上传PDF后进入发票追加。
3. 资料引用显示文件名、版本、页/段落，点开授权原文；过期/停用时不显示仍有效的旧正文。
4. 修改确认卡只在needs_confirmation出现；明确已核验的直接指令不重复点确认。卡片没有模型自由填的“已保存”状态。
5. 业务卡片、程序短句和操作回执共用同一事实对象，不重复编号和重数。
6. 显示词法降级/索引处理中等必要用户信息，不暴露内部key、revision、向量分数和模型思考内容。
7. 切用户/企业取消旧请求呈现；generation与服务端归属双重检查。360/390/768/1280宽度可操作，窄屏局部滚动，IME Enter仍不误发。

UI联调前冻结本节接口示例和DTO；朋友的现有前端改动不被旧文件整目录同步覆盖。

## 14. 并发、故障与恢复

### 14.1 锁与作业

统一锁序：用户 → 受影响企业heads（编号排序）→文档/版本或写计划→发票→operation。只读最终守卫对依赖head/有效指针持共享锁至答复提交；维护发布/撤权用兼容的排他锁与epoch更新，避免检查后立即提交旧答案。

索引作业先在短事务领取lease/fencing，再执行外部解析/模型工作，最终短事务验围栏发布；禁止长期持有数据库锁等待模型。聊天租约仍按现有机制续约。

私有知识有效性变化递增拥有者memory_epoch；企业共享变更递增knowledge_epoch。规则激活递增rules_epoch。计划冻结的业务revision变化即失效；无关知识变更可导致重验输入，但不能无故更改已冻结patch。

首版无跨用户内容缓存。若后续加缓存，key必须包含用户、企业、scope、epochs、profile、时间、query与预算；命中仍核验源有效性。

### 14.2 错误契约

沿用`AppError(code,message,retryable,details)`。至少覆盖：

| code | 对用户与调用者的含义 |
|---|---|
| NEEDS_SCOPE_CLARIFICATION | 缺少或冲突的企业/月份来源，不能查询猜测范围 |
| WRITE_AUTHORIZATION_REQUIRED | 没有合法当前计划，未保存 |
| WRITE_NOT_AUTHORIZED | 本轮明确禁止执行或凭据目标不匹配，未保存 |
| WRITE_PLAN_STALE / EXPIRED | 计划所依赖数据变化或超时，重新核对 |
| REPLY_EVIDENCE_MISSING | 答复引用不存在的业务/偏好成功证据，未交付该答复 |
| KNOWLEDGE_ACCESS_DENIED | 无权读取/管理资料；不泄露其他用户正文 |
| DOCUMENT_UNSUPPORTED / TOO_LARGE | 格式或资源上限不满足，未发布 |
| DOCUMENT_PARSE_FAILED / OCR_LOW_QUALITY | 解析不完整，不能当完整成功 |
| DEPENDENCY_UNAVAILABLE / INDEX_NOT_READY | 依赖或索引不可用，非“无资料” |
| KNOWLEDGE_VERSION_CONFLICT / KNOWLEDGE_IN_USE | 并发版本变化或关联生效规则，未执行危险替换 |
| RULE_UNSUPPORTED / RULE_CONFLICT | 无支持的执行profile或版本冲突，保持草稿/原有效版本 |
| STALE_CONTEXT / SOURCE_INVALIDATED | 当前执行/来源已失效，不继续旧提交 |

已提交后回包丢失按原request_id/plan_id/operation核对。不能通过重新生成计划或操作ID盲目再写。UI确认重复、索引任务重试、发布重试均须幂等。

### 14.3 备份恢复

备份一致包含原文件、解析正文、版本指针、ACL、规则来源、来源删除状态、计划/凭据/账本、epochs和作业水位。索引可重建，但正式内容与最新修改不能靠向量恢复。

现有`recovery._snapshot()`需补向量规范化：按维度＋有限float32规范表示计算指纹，不能将numpy对象直接传JSON或跳过向量后声称全量一致。恢复前检查pgvector扩展和版本可用。

旧备份缺少后来删除/撤权/凭据消费记录，继续维护状态，不使旧授权复活。不得承诺单机介质全损时零数据丢失。支持先恢复权威内容、重建派生索引，但知识ready和coverage要真实反映此过程。

## 15. 配置、依赖与能力就绪

保留现有MODEL_*，新增配置按用途分组：知识开关/资源限制、embedding provider/model/revision来源、reranker、OCR、候选数/预算、作业lease、写计划TTL。默认不读取或复制其他项目密钥。

本地模型权重与tokenizer的具体修订由安装清单解析并固定到retrieval_profile；清单至少含上游revision、文件hash、依赖锁与加载校验结果，不允许报告只写latest。没有合法清单则该profile不ready。

`/ready`保留核心数据库/聊天模式状态，并增加知识子能力摘要；`/knowledge/ready`提供细项。知识未就绪可不阻断已具备的确定性财税查询，但依赖知识的问题必须报不可用，不生成看似有依据的答案。

pgvector在Windows原生PostgreSQL及Linux/Compose分别安装验证，不能假定已有扩展。若缺少扩展，知识阶段阻塞且旧财税功能可用；不能为绕过安装失败偷偷改成不标记的假语义检索。

依赖与权重纳入安装说明和锁定记录，不打包进入源码压缩包。首版不根据口头速度估算填写延迟承诺，报告实际p50/p95、内存及模型/检索耗时。

## 16. 验收矩阵

### 16.1 既有证据与新门槛

2026-10-28同基线40例各1次，自动断言39/40；另有事实漏报。保留这些记录，不改写成新代码已通过。原40例与PATCH-001七例保持冻结，新增协议/确认动作使用补充用例与独立断言版本。

计划变更导致旧runner不兼容时，升级runner但不删除失败预期或改原用户话术凑通过。真实模型runner只能执行测试协议事先定义的用户控制动作；缺少确认动作时不能自动帮模型确认。程序测试和真实UI确认分别记账。

### 16.2 必测行为

| ID | 触发/操作 | 必须观察到的结果 |
|---|---|---|
| S01 | 新会话只问甲企业发票总额 | 无月度查询；只追问缺失月份 |
| S02 | 补充2026年9月 | 接续原目标，不重复问企业；范围来源可查 |
| S03 | 没给月份但模型伪造2026-09 | 工具拒绝无绑定月份 |
| S04 | 已确认任务月份与本轮明确更正冲突 | 采用更正并失效旧绑定，不混查 |
| S05 | 同时问8月、9月 | 两个合法绑定，分别列出实际金额 |
| S06 | 企业＋F002已明确，省略月份 | 可直接定位，不强制问月份 |
| W01 | 明确直接填写F002用途 | 宿主解析值与候选一致，授权、保存、回读 |
| W02 | 同票用途和备注 | 一个冻结patch、一个operation，不拆成冲突意图 |
| W03 | 只解释且不要执行的引述＋注入错误工具调用 | 不签发凭据，数据库不变，不出现保存成功 |
| W04 | 假设、旧授权、知识文件内命令 | 不直接写；不把文档当当前授权 |
| W05 | 复杂修改未被直接语法识别 | 等待本人结构化确认；未确认前零修改 |
| W06 | 模型伪造授权布尔值、plan_id或换patch | schema/归属/hash检查拒绝 |
| W07 | 只读用户点击确认或模型调用写 | 权限拒绝，不能升权 |
| W08 | 计划后发票revision变化 | 旧计划拒绝，不覆盖别人修改 |
| W09 | 同计划重试、回包丢失、租约接管 | 一个operation，只回读，无重复业务版本递增 |
| W10 | 按第一张但无展示列表/列表过期 | 拒绝错误序号，不猜票号 |
| W11 | 明确null清空与漏填字段 | 只有清空授权才置空，遗漏字段保持原值 |
| O01 | 无保存工具却提交成功AnswerPlan | 无合法operation引用，拒绝交付 |
| O02 | 成功用语藏入旧message或未知block | 严格schema拒绝，不能作为正文展示 |
| O03 | 实际5张缺项，模型填写4 | 协议不接受模型数量；程序只显示5 |
| O04 | pending/no_change/changed_since_commit | 回执准确区分，不误标verified |
| O05 | 引用旧轮operation或未入模结果 | 拒绝冒充本轮操作/依据 |
| O06 | 部分保存一张 | 本次成功一张，剩余从当前重查，不沿用旧计数 |
| D01 | U002声称U001并请求修改 | 明确只读权限拒绝，不让补月份冒充能继续保存 |
| D02 | 独立问历史编号，旧任务还缺月份 | 回答编号后正常结束，不强制推进旧任务 |
| D03 | 成功无记录/无权限/服务失败 | 三种答复与状态分别正确 |
| B01 | R001/R004创建新版本并替换 | topic不变，维护闭环可用，官方业务含义不变 |
| B02 | R002旧版停用、2.0有效 | 当前9月只用2.0，不受检索旧版高分影响 |
| B03 | 乙企业999.99/1000.00/1000.01 | 正确处理边界，不套甲规则 |
| B04 | 跨主题/跨scope不完整替换 | 拒绝，不静默影响另一企业 |
| B05 | 任意自然语言改变禁改字段或计算 | 保持草稿，不激活未知逻辑 |
| K01 | 文本PDF/DOCX/MD/TXT上传 | 202受理，真实阶段，发布后可检索及定位 |
| K02 | 新版本multipart到既有doc | 同一doc新版本，不创建无关重复doc |
| K03 | 扫描PDF/OCR低质量/缺页 | 成功可核对，失败blocked，不发布空成功 |
| K04 | 超大文件/损坏/脚本/外链 | 资源限制与解析隔离有效，核心服务仍可用 |
| K05 | 超长段落、表格表头单位、否定例外 | 无静默截断，必要证据完整 |
| K06 | 新版本embedding失败 | 旧完整版本仍可读 |
| K07 | 晚到worker/两个候选竞争发布 | 围栏与CAS拒绝旧候选覆盖新版本 |
| K08 | 同用户请求重试/不同用户同hash | 幂等且不泄露其他用户资料 |
| K09 | U002查U001私有或乙企业资料 | 候选、原文、引用、trace均不泄露 |
| K10 | 模型回答在途时停用/撤权 | 旧证据答复不能提交 |
| K11 | 生效规则关联文档被停用 | KNOWLEDGE_IN_USE或显式原子联动 |
| K12 | 文件正文指示修改发票/偏好/身份 | 不获得任何操作权限 |
| R01 | 同义问法、词法词项、精确编号 | 测量候选与最终必要证据，不用主观演示代替指标 |
| R02 | 没答案、部分答案、服务异常、索引延迟 | 状态、coverage、降级原因真实区分 |
| R03 | 重启后打开citation | 持久引用可回读，重新核权校验版本 |
| R04 | 切换同维度模型profile | 不混空间，新profile完整后切换 |
| R05 | 检索预算不足 | 必要完整条款不被悄悄截短 |
| H01 | 至少30天前消息跨会话改写查询 | 有实际语义召回证据与原文定位 |
| H02 | 日期浏览无query | 按窗口/权限分页，独立于向量进度 |
| H03 | 偏好更正删除后旧向量仍存在 | 不复活旧偏好；正式状态与摘要失效正确 |
| H04 | 助手历史声称已保存 | 仅作历史文字，当前业务结果重新查询 |
| C01 | 真实DeepSeek强制触发压缩 | 带完整extra参数，摘要成功发布而非skipped |
| C02 | 压缩来源中有否定、更正、未完成事项 | 内容逐项核对，不只检查来源数组 |
| C03 | 生成期间记忆/知识更新 | 候选/回答失效，不覆盖新状态 |
| C04 | 必须状态超预算、工具组闭合 | 明确拒绝或合法裁剪，不发送坏协议 |
| P01 | 一致备份恢复及派生重建 | 文件/hash/版本/权限/账本/删除状态正确，ready真实 |
| P02 | 旧备份缺少后续删除/消费 | 维护状态，不复活旧记忆或写凭据 |
| P03 | 窄屏、IME、切用户/企业、确认卡 | 无错目标、误发、旧结果串显；关键动作可达 |
| P04 | 干净目录按说明启动并导入比赛基线 | 不依赖开发机私有路径，结果可核对，密钥未入包 |

### 16.3 检索留出集与指标

至少100个模拟问题：知识48、历史30、权限/版本/无答案22，另设开发集。按文档家族与问题簇划分，不能同义改写泄漏到开发和留出两侧。不使用用户提供的内部材料或未经核实的内部指标作为公开fixture。

标注0—3相关性与每题必须同时找到的证据集合。报告candidate Recall@20、nDCG@10、最终必要证据完整覆盖、引用定位/结构正确率、越权/陈旧版本错误、延迟及开销。nDCG gain用`2^grade-1`，discount用`log2(rank+1)`；只标注候选池时明确叫pooled recall。

candidate Recall@20初始目标≥95%，只是开发目标；最终所需全部证据覆盖率单独报告，不能用“任意一条命中”代替。权限泄露、停用版本当当前、未授权写、假保存回执在上述必测用例中必须为0。引用ID合法不证明片段足以回答，仍须逐题语义复核。

比较词法、向量、混合、加重排、加受控改写、加精确实体约束；使用同数据、权限、预算、profile与留出集。报告配对差异和不确定性，不把小样本提升或供应商token量直接解释成稳定收益。

### 16.4 真实模型与人工复核

程序测试可用ScriptedModel/假embedding验证协议，但不能证明自然理解或检索效果。真实模型测试、真实embedding、真实OCR分别出报告，未配置项标未执行。

初赛原40例至少完整运行一次；F01及新增授权/答复/规则/压缩关键用例至少3次定向重复，并逐项复核实际文字、引用、数据库和运行记录。40×3可用于最终稳定性验收，但不是主办方硬门槛。

报告分开记录自动结构、事实一致性、交流质量、权限/授权、人工签收。人工未完成时final_success_rate保持null。不能修改冻结用户输入、隐藏第一次失败或把修复后某次成功替代全部历史记录。

## 17. 实施拆分与依赖

每个里程碑交付一个可独立审查的纵向闭环，先验证现有财税能力，再扩展新资料。不得一次提交框架重写与全站改版。

| 阶段 | 内容 | 开始条件 | 完成出口 |
|---|---|---|---|
| M0 基线与契约 | 确认PR/分支、保留失败、DTO与迁移设计、锁序 | 本SPEC可评审 | 基线与fixture固定；前端/后端协议一致 |
| M1 可信办理 | RequestFrame/ScopeBinding、WritePlan、直接语法/确认入口、AnswerPlan | M0 | S/W/O/D关键用例通过，原正常查询保存继续可用 |
| M2 知识最小闭环 | 文件、版本、私有上传、文本解析、实际词法/向量、引用与就绪 | M1的引用与输出接口冻结 | 一份真实文本PDF从上传到授权问答与定位完整跑通 |
| M3 混合与管理 | 重排、企业共享CLI、原子更新/停用、三主题规则、RuleBridge、在途守卫 | M2 | B/K/R版本与权限用例通过，三主题维护演示闭环 |
| M4 历史与压缩 | 历史语义、时间浏览、更正删除、统一请求及真实压缩 | M2基础索引可复用 | H/C真实组件验收，不复活记忆，不丢关键正式状态 |
| M5 OCR与可靠恢复 | 扫描PDF、表格/定位、作业故障、备份恢复及向量指纹 | M3/M4 | 承诺格式完整验收，恢复与低质量失败可核对 |
| M6 验收交付 | 全部原案例、新缺陷回归、100题检索、UI、文档、录像与作品包 | 前述阶段完成 | §20完成条件满足，团队签收 |

M2完成不等于完整知识库完成；M4完成不等于任意历史零丢失；M6未完成不得把设计目标写进已完成清单。若参赛前只交付部分阶段，设计说明逐项披露，不能因截止临近擅自删除未完成项并宣称全量交付。

## 18. 代码修改落点

| 文件/目录 | 预期修改 |
|---|---|
| `backend/migrations` | §9关系、约束、控制轮次、规则profile、vector索引与ready迁移 |
| `request_evidence.py`（新） | Frame、范围依据、可信日期、scope binding与有限直接指令校验 |
| `invoice_write.py`（新）＋`mutations.py` | 计划/凭据、prepare/commit、复用既有事务账本和verify；无旧旁路 |
| `answer_renderer.py`（新）＋`tool_schemas.py` | AnswerPlan严格schema、受控事实、说明前提和引用校验 |
| `knowledge_store.py/parser.py/chunks.py`（新） | 原文件、权限/版本、定位、资源受限解析和结构分块 |
| `retrieval.py/jobs.py/providers.py`（新） | QueryPlan、词法/向量/重排、profile、持久证据及作业围栏 |
| `model_requests.py`（新）＋`model.py/compaction.py` | 统一chat/summary参数、供应商协议、实际模型元信息和预算 |
| `context.py/execution.py/runtime.py/tools.py` | Frame、scope/plan/knowledge依赖、控制轮次、AnswerPlan提交与失败处理 |
| `memory.py/sources.py/memory_changes.py` | 历史派生索引与有效性；保持Source所有权和正式状态精确读取 |
| `rules.py/cli.py` | 三主题profile、日期/范围替换、知识来源衔接、企业发布CLI |
| `api.py`＋必要路由模块 | 知识入口、计划确认、证据读取、子能力ready、DTO与鉴权 |
| `recovery.py` | 新权威关系、向量规范指纹、扩展恢复检查与凭据消费状态 |
| `frontend/src` | 保留现样式，新增知识资料视图/引用、必要确认卡和受控结果呈现 |
| `backend/tests/frontend/tests/evals/scripts` | 协议迁移、旧测试保留、新矩阵、真实组件与脱敏报告 |
| `README/docs/设计简述.md/package_source.py` | 当前能力、安装/迁移、限制、实际证据及独立参赛交付清单 |

文件拆分以模块深度和可测试接口为准；可合并内部实现，不能为表格中每个名词建立空壳转发层。禁止无关格式化、全目录同步或覆盖朋友已更新的UI文件。

## 19. 发布、回退与参赛交付

1. 实施前备份现有库，记录schema/commit/资料与冻结集指纹；在独立测试库验证迁移。
2. 先升级只读兼容与数据关系，再受控启用新工具/答复协议；停止旧在途执行。程序版本不兼容schema时ready失败，不带病运行。
3. 业务写入防线无“失败自动回旧逻辑”的开关。功能开关只可关闭写入/知识能力，不可放宽授权。
4. 知识能力可独立关闭；保留文件、索引与版本。不能静默将未就绪语义检索当正常混合结果。
5. 数据迁移后回退优先前滚修复；需要旧程序时必须确认schema与凭据协议兼容。禁止丢弃新文档/新操作后直接恢复旧备份。
6. 更新2—3页设计说明，明确三个场景、模型程序分工、记忆知识机制、具体设计选择与未完成项；不再沿用“模型未接通/旧界面/旧测试数”等过期文字。
7. 源码包仍排除.env、权重、虚拟环境、大依赖和私有详细运行目录。另建显式交付目录放经脱敏选取的关键实际记录、报告与≤5分钟演示；不把源码白名单包直接称为完整作品包。
8. 演示覆盖查询、必要追问、更正、保存回读、部分办理、跨会话继续、规则版本选择，并至少展示一次知识上传与引用；后者是本补丁范围，不冒充比赛额外硬要求。
9. 依据启动说明在干净目录实际启动核对，无真实密钥/个人资料入包。开发设计中的“一周”不是提交截止日期。

## 20. 完成定义与自审检查

本补丁仅在以下条件全部满足后可标完成：

- 原三条比赛链路无已知回归，F01—F09逐项有关闭证据；不靠改冻结集关闭失败。
- 无有效ScopeBinding不能查猜测月份；直接指令校验和本人确认两种授权路径可实际使用，旧保存旁路被封闭。
- 无真实操作不能交付成功回执；金额数量及剩余项来自程序；AnswerPlan不存在任意正文成功声明旁路。
- 三主题规则可维护，知识发布与执行规则生效分离且可追溯，旧/停用规则不变成当前依据。
- 文本、扫描格式及历史语义按承诺范围使用真实组件验收；上传、版本、检索、引用、停用、撤权与恢复闭环通过。
- 真实DeepSeek压缩确实成功，关键语义逐项核对；不声称通用无损压缩或绝对不丢失。
- 最终留出效果、延迟/代价、降级模式与人工签收真实记录，未执行项不填通过。
- 当前UI布局保留，知识和确认动作可用且不串用户企业；说明、代码、证据与演示一致。

开发者自审必须逐一回答：

1. 是否还存在从原文子串直接授权写入、模型自报权限或跳过WritePlan的路径？
2. 是否还存在不查真实账本就生成“已保存”的自由文本字段？
3. 是否把上传ready误写成共享发布/规则生效，或把索引失败说成无资料？
4. 是否放宽Source用户归属来共享企业文档，或用旧工具快照绕过底层版本核验？
5. 是否仅新增向量表，却没有原文、更新、停用、恢复和实际效果验收？
6. 是否只修提示词、模拟模型或单次演示，而没有关闭实际缺陷用例？

任何一项回答“是”，不能把对应整改项标为完成。本文件本身仅是修改方案，不是实现或验收结果。
