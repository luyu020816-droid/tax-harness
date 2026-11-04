# DeepSeek Harness 上下文机制：原始实现调研

调研日期：2026-11-03。只参考 deepseek-ai/deepseek-harness 官方仓库。通过 `git ls-remote` 取得 master 提交 `8bd073392eef6f54e921a093b95076248eb08cbb`，并直接下载该提交的核心会话、压缩、权限与检索源码核实。本文是研究记录，不是本项目已实现能力。用户已确认参考对象是 DeepSeek Harness；本次不扩展为 Codex 对比。

## 1. 先区分四件事

DSH 的原始会话日志、当前模型可见历史、全文搜索索引、模型搜索工具分别承担不同职责。只保存 JSONL 不会自动获得历史搜索；全量日志也不等于每次全量发给模型。它不是固定截取最后 12 轮，而是从日志推导当前有效消息，再按上下文压力进行压缩。[会话实现](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/core/session/src/index.ts#L999)

## 2. 日志与当前模型视图

会话接受事件时会复制 JSON、冻结已接受的数据，追加到日志；`deriveMessages()` 由当前 surface 推导模型消息。压缩通过追加“替换某段可见历史”的事件改变 surface，旧事件仍在原始日志里。因此“可见历史变化”与“原文被删除”不是一回事。[append 实现](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/core/session/src/index.ts#L817)、[会话契约](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/core/session/README.md)

这可以映射到本项目的 PostgreSQL：追加原始消息/关键事件，单独维护当前上下文投影。无需引入 JSONL 与数据库双写，也无需把整个财税系统改成事件溯源。

## 3. 压缩由 token 压力驱动

DSH 衡量当前模型容量、输出预留、额外余量与消息成本。该提交的默认阈值比例为 0.8，近期原文保留比例为 0.16，额外余量为 65536 token；模型可覆盖策略。触发后处理最早的可安全压缩区间，保留近期原文。它还支持确认的上下文溢出后的有限重试。固定数值不能照搬到本项目当前较小的 INPUT_BUDGET，应根据完整请求和实际成本评测设置。[策略源码](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/compaction/compaction-basic/src/config.ts#L145)、[实现说明](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/compaction/compaction-basic/README.md)

压缩不是保证语义无损。重要方案、否定、更正与未完成事项应在专项评测中验证，必要时能按来源回读完整原文，不能用来源 ID 齐全代替语义正确。

## 4. 不切断工具调用与结果

DSH 基于 surface 顺序计算工具调用平衡；安全切点不能横跨尚未收到结果的工具调用。缺少对应调用的孤立结果会被拒绝。它可以压缩一轮内部已经闭合的工具步骤，不要求只能整轮切割。这比固定消息条数更适合长工具任务。[工具配对源码](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/compaction/compaction/src/tool-pairing.ts#L33)

本项目不应把失效的历史工具结果恢复为当前业务事实。旧查询可保留为历史证据，当前金额、权限、规则与保存结果仍需现有版本检查和工具回查。

## 5. 压缩事务、失败与恢复

压缩先追加 start，异步生成摘要后重新校验选定区间，提交 summary 及替换，再追加 end。源码区分摘要失败、提交失败与持久化失败；失败尝试保留明确错误，未闭合 start 可被识别。自动 pre-step 压缩失败会记录警告后继续该轮，不能推断后续请求一定能够成功；溢出恢复只在发生可证明的有效缩减等条件下有限重试。[压缩区间事务](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/compaction/compaction-basic/src/region.ts#L210)、[失败处理](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/compaction/compaction-basic/src/index.ts#L156)

本项目可复用 PostgreSQL 事务、租约和 fencing token：候选成功并再次校验范围后才发布。压缩失败保持旧投影；若旧投影仍超预算，则明确暂停或返回容量错误，不静默裁掉未覆盖原文。

## 6. 历史搜索、回读与默认开关

查询模块提供会话筛选、事件搜索、原始事件精确读取及邻近窗口。SQLite FTS5 索引是独立派生数据，查询前与日志进行一致性协调；命中被摘要遮蔽的事件仍可回读。默认 tokenizer 匹配 token/短语，不能因此宣称任意中文同义句都能找到。[搜索后端](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/session-query/session-query-sqlite/README.md)、[query 源码](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/session-query/session-query-sqlite/src/query.ts)

模型侧实验工具包括 session_search、session_event_search 和 session_event_read 等；独立 bundle 在 Web/Desktop 默认关闭、首次检索才打开内容索引，重启后可重建。不能宣传“DSH 默认每轮自动搜索全部过去会话”。[实验开关](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/experimental/session-search/README.md)

本项目应将聊天历史检索与知识资料检索分开命名、分开索引和引用；可以用现有 PostgreSQL 建派生索引，不必引入 SQLite。确切日期、词面、票号检索与中文语义检索应分别报告效果，不承诺百分百召回。

## 7. 权限不能直接照搬

DSH 模型工具的跨会话权限主要按调用者与目标 cwd 完全相等判断；无 cwd 只能看自己。底层可信查询模块本身不代办调用者授权。我们需要 user_id、企业授权、会话删除、来源抑制、引用回读时重新核权，而非 cwd。[权限实现](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/experimental/tool-session-query/src/workspace-access.ts#L74)

## 8. JSONL 是存储适配器，不是记忆本身

JSONL 后端负责按批追加与 fsync、处理尾部未完成记录和格式版本。恢复工具结果时区分尚未开始与结果未知，未知副作用要求核查，不能盲目重放。该机制值得对照本项目现有操作账本，但存储介质无需照搬。[持久化契约](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392eef6f54e921a093b95076248eb08cbb/packages/session/session-persistence-jsonl/README.md)

## 9. 建议采用与明确不采用

采用：原始日志与当前投影分离；token 驱动保留和压缩；工具配对；候选原子发布与失败可见；历史搜索后精确回读；索引可重建。保留本项目数据库、操作账本与当前数据回查。

不照搬：JSONL 主存储、Cordis 插件体系、cwd 授权、DSH 默认余量，以及实验工具默认可用的假设。迁移不能伪造过去未记录的中间工具调用，只能标记旧数据覆盖范围。需要新回归验证摘要覆盖、删除与撤权、断点恢复、超大单条消息、同义查找和无答案，不沿用旧历史 RAG 成绩证明新方案。
