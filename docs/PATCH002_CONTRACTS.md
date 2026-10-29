# PATCH-002：办理协议与模块边界

日期：2026-10-29。协议：trusted-execution-v1，数据库 schema 4。

本文件描述路线图第1—2步的已实现接口。知识文档、向量索引和OCR不在本批实现中。完整需求仍以SPEC为准。

## 1. 主链路

`当前用户原话 → RequestFrame → ScopeBinding/实际查询 → WritePlan → 本人授权 → operation账本 → AnswerPlan → 程序渲染`

模型负责理解、选工具、组织证据；金额、缺项、权限、写授权与保存状态由宿主核验。正式状态继续使用现有PostgreSQL、Source、任务和操作账本，不建立另一份保存真值。

| 模块 | 职责 | 不承担的职责 |
|---|---|---|
| `request_evidence.py` | 从当前原话、有效任务和成功查询来源绑定企业与月份 | 不根据样例或当前日期补月份 |
| `write_commands.py` | 有限完整语法核对原话与冻结patch；识别命令外层否定 | 不替代模型的总体意图理解 |
| `invoice_write.py` | 准备、核验、确认、撤销计划；创建持久控制轮次 | 不维护另一套业务账本 |
| `mutations.py` | 按计划执行、记录操作、同事务消费、回读和幂等重试 | 不接受无计划的自由patch直写 |
| `tool_schemas.py` | 严格工具参数与AnswerPlan DTO | 不接收任意message或未知block |
| `answer_renderer.py` | 按真正入模的来源及账本生成文字、数量、卡片和追问 | 不信任模型提供的业务数字或成功措辞 |
| `runtime.py` | 有界模型循环；本人确认走程序执行；发布前再次守卫 | 不让模型伪造确认来源 |
| `WritePlanCard.tsx` | 展示前后值、确认/撤销、读取计划状态、恢复结果 | 不自行推断后端是否已经保存 |

## 2. 请求范围

每次装配输入生成`request_frame`，包含当前source ID/revision、持久接收时间、Asia/Shanghai日期、明确企业及`available_scope_refs`。相对日期按该条来源的接收时间计算；仅显示当前日期不构成月份授权。

月度工具必须提交`company_id + period + scope_ref`。`scope_ref`绑定本人、本轮、该企业月份和来源修订，不能跨轮复用。`resolve_scope(company_id,period)`由宿主追溯依据，不接收模型自称已确认的布尔值或自造basis_refs。

有明确年份时“改查9月”可复用已确认年份；未确认年份则追问。连续复用时保留来源链，修改来源revision后拒绝旧依据。用户同时明确查询两个月时分别绑定；“8月还是9月”不能任选一个。

常见拒绝：`NEEDS_SCOPE_CLARIFICATION`、`TARGET_SCOPE_MISMATCH`、`SOURCE_INVALIDATED`。企业＋明确票号的定位/权限拒绝不要求机械补月份。

## 3. 修改计划

模型先`find_invoices`读取当前revision，再`prepare_invoice_write`，参数为企业、票号、明确给出的purpose/note、expected_revision、当前原话source_refs及可选selection_ref。

同一原话、同企业、同票的patch冻结一次。双字段必须合并；改变冻结内容返回`INTENT_CONFLICT`，需要用户新消息更正。遗漏字段不写入；null只接受明确清空语句，并走本人确认。

| status | 含义 | 下一步 |
|---|---|---|
| authorized | 有限完整语法与patch相符，或本人已确认 | 当前授权轮可提交plan_id |
| needs_confirmation | 候选不能由直接语法充分核验 | 展示卡片，等待本人界面确认 |
| prohibited | 否定、假设、只解释或纯引文等不能执行 | 不生成可确认的执行入口 |
| consumed | 已关联原operation | 查原结果，不能再次增加业务版本 |
| revoked / expired | 撤销或过期 | 不执行；用新消息重新核对 |

返回的计划包含`plan_id / revision / intent_hash / company_id / invoice_id / patch / before_values / expires_at / status`及已有控制轮次和operation ID。默认有效期10分钟。有效期也由后端检查，不能依赖浏览器时钟。

直接语法支持明确票号/有效展示序号、赋值和逗号连接双字段，例：`把甲企业F002用途填写为办公耗材采购`、`甲企业F002用途填办公耗材采购，备注填行政部确认`、`先只填F002用途为办公耗材采购，其余还没确认`。自然语言不完全匹配时不能直接授权。字段值内出现“不要”不等于命令外层禁止执行。

### 本人确认HTTP

`GET /api/v1/write-plans/{plan_id}`读取本人计划。

`POST /api/v1/write-plans/{plan_id}/confirm`：

```json
{"expected_revision":1,"intent_hash":"冻结计划返回的64位hash","request_id":"客户端唯一请求号"}
```

后端锁定用户→企业→计划，检查权限、hash、revision、有效期、来源修订、企业、发票revision和规则版本，创建`write_confirmation`轮次及`user_action`来源，返回`turn_id`。重复确认返回同一轮次。该轮次直接按冻结计划执行，不调用模型重新决定目标。成功后收束原待确认事项。

`POST /api/v1/write-plans/{plan_id}/cancel`提交`request_id`，撤销未消费计划并留审计来源；不撤销已提交的业务。确认/撤销并发沿用用户锁，不能产生第二份授权。

模型提交接口仅为`update_invoice({"plan_id":"真实ID"})`，不再接收自由patch。核心保存函数同样要求有效计划，不能通过直接调用旧函数绕开。

## 4. AnswerPlan

最终答复只接受严格JSON，不接受旧message、自由数字、保存状态或自由追问。`blocks`可为空，但必须有必要的clarification；completed空白答复会拒绝。

```json
{"blocks":[],"status":"waiting_user","clarification":{"intent":"query_month","company_id":"C001","missing_fields":["period"]},"resume_turn_id":null}
```

| block | 模型提供 | 宿主核验和呈现 |
|---|---|---|
| result | 真正入模的result_ref及匹配view | 真实金额、总数、分页数量、规则名称版本、卡片 |
| operation | operation_id | 本轮真实账本、归属、提交与核验状态 |
| preference_receipt | 实际result_ref与action | 本轮正式偏好操作提交证据 |
| history_quote | 本轮历史检索返回的source_id及逐字引文 | 权限、原文匹配、角色、日期，逐行引文标识 |
| confirmation | 本轮实际待确认plan_id | 本人计划仍有效且needs_confirmation |
| explanation | 受控说明code | 必须符合实际权限、工具错误或有效规则前提 |

结果ref由宿主分配，模型收到的JSON、Source原文和trace共享同一身份。未入模、被裁剪或其他轮的结果不能冒充当前事实。数量由程序读取，协议中没有“模型填缺项数量”的字段。

权限拒绝/无记录/失败分别处理。必要追问字段限定企业、年月、票号、用途、备注、事项和本人确认，不提供“是否长期跟踪”等无关选项。长期偏好仍由模型识别并调用正式偏好工具；普通应答不等于偏好已保存。

`get_invoice_operation(operation_id)`只读核验本人旧操作，之后才允许以“原操作核验”呈现旧操作，不能冒充本轮新保存。prepared/未知、rejected、committed待回读、no_change、verified、changed_since_commit各自呈现；没有账本不允许成功回执。

API仍返回`final_message / final_cards`供客户端读取，内容均由宿主渲染。本人确认执行快照标记`control_execution`，运行详情不将其计为模型调用。

## 5. 迁移与隔离部署

1. 停止目标实例所有旧worker，备份数据库；不能混跑旧工具协议和新worker。
2. 同版本代码执行`python -m app.cli init`应用004迁移。旧queued/running轮次被停止并增加围栏，保留已有账本与任务；未完成请求重新提交。
3. 构建前端并启动同版本后端，确认ready返回成功。旧本地登录cookie改为按host/port/db隔离，需重新登录。
4. 验证直接保存、复杂确认、只读拒绝和操作核验。迁移后回退旧可执行文件不是受支持的回滚方式，按备份与受控恢复流程处理。

备份快照原有逻辑枚举全部public表，新范围和计划表会进入数据快照及指纹校验，不另建仅保存在内存的授权状态。本批不代表已经完成后续全量故障恢复演练。

开发与测试使用54338独立实例；朋友的8104服务和54337数据库保持原工作环境。本文件不表示已合并main或已发布知识库。
