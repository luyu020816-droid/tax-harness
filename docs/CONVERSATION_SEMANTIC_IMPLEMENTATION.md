# 聊天语义索引实施记录

依据：[SPEC](superpowers/specs/2026-11-04-conversation-semantic-index-spec.md)。当前默认off，尚未通过语义质量与人工验收，不宣称已上线。

## S1/S2基础检查点

新增独立profile、作业、片段向量表及事务入队触发器；原文仍追加保存，旧知识混合入口不启用。后台支持补偿扫描、幂等入队、租约接管、最多3次退避重试和原子发布。增加conversations-index backfill/status/retry显式CLI。尚未接入应用生命周期和混合查询。

隔离库新增6项测试通过；会话、摘要与恢复31项回归通过。首次4项失败保留在.local/semantic-s1.log；根因为发布时直接锁授权表需要UPDATE权限，改为不返回正文的受限锁函数，不开放授权表修改。复验见.local/semantic-s1-fixed.log及semantic-s1-regression.log。

S3混合搜索、S4引用/UI、S5真实质量和故障验收继续实施；主库尚未执行015迁移。用户偏好改进不在本次范围。
## S2/S3/S4功能检查点

已接入应用启动/停止、补偿索引、精确授权集合内向量排序、词法融合、支持度文件校验、稳定候选游标和off/shadow/on。未校准策略或依赖不可用时明确partial回退；无词日期查询保留原分页。跨用户旧游标明确拒绝，不重新执行一次查询。

相邻消息只给导航，实际引用仍须回读；界面复用现有来源卡片，失效时清除展开正文。运行洞察直接记录本地查询向量、词法/向量候选、重排和原文核验阶段，本地计算不冒充远端Token费用。

新增14项专项通过，既有会话与专项29项通过，API与专项17项通过；浏览器3项及TypeScript/Vite构建通过，390宽度截图已检查。程序测试使用明确标记的假向量，不作为真实语义效果证明。原始游标失败及复验保留在.local/semantic-s3.log和semantic-s3-fixed.log。

完整后端回归已启动，真实本地模型质量、独立留出、故障及迁移仍待本轮后续验证；当前保持off，不修改本地真实.env，不宣称已通过S5。
## 本批交付与开关状态

核心链路已接入。演示默认 `off`，沿用日期和词法检索。`shadow` 用于构建索引和比较候选，用户仍看到词法结果。`on` 由维护者在验收后显式配置。当前演示环境保持关闭，候选策略文件只留在隔离验证目录。

| 验证层 | 结果 |
|---|---|
| 最终程序回归 | 后端 353 项，浏览器专项 3 项，TypeScript/Vite 构建通过 |
| 直接检索 | 60 道合成复核题。词法改写命中 0/20，混合 19/20，Recall@5 为 0.95，MRR 为 0.925。日期 20/20，多候选 10/10，无答案误报 0/10 |
| 真实 Agent | DeepSeek V4 Flash 三轮结果分别为 30/36、34/36、32/36。最新轮长原文和历史指令各 3/3，选择方案 2 次、旧日期 1 次、索引未完成 1 次未满足预期 |
| 安全 | 最新轮 20/20 权限负例通过，36 次运行未产生意外业务写入 |
| 迁移 | 本地演示库已升级到 015。迁移前在独立恢复库核对 60 张表，原字段指纹一致 |
| 人工验收 | 60 题为预先冻结的合成草案，待人工核验标签。规模与故障验收尚待补齐 |

最新失败分别表现为选择方案的引用未命中期望原文、明确旧日期时未调用历史工具、索引未覆盖时仍附带了不相关引用。早期长消息上下文超预算的轨迹保留。修复依据是只提供本轮仍允许调用的工具，并保留原文片段、字符偏移和原消息入口。保留三轮结果用于观察模型波动。

脱敏汇总见[结果 JSON](../evals/conversation-semantic-v1/results.json)，原题见[合成题集](../evals/conversation-semantic-v1/review-cases.json)。本地模型使用了相同输入的真实推理缓存，耗时不代表生产延迟。原始轨迹保存在本机 `.local/semantic-real-v1/` 与 `.local/semantic-runtime-v1/` 至 `.local/semantic-runtime-v3/`。后续验收优先独立核对 60 题的原文、日期、权限和候选集合，并用新题复测三类失败。现有原生推理崩溃根因仍待定位。

## 本地操作

从仓库根目录使用已有虚拟环境。索引使用仓库配置的本地模型。诊断入口是现有的「运行洞察」。

```powershell
Push-Location backend
..\.venv\Scripts\python.exe -m app.cli conversations-index status
Pop-Location
```

隔离评测会重置指定测试库，串行运行，并使用新的输出目录。第二条命令使用本地配置的 DeepSeek API 并产生真实调用。

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_conversation_semantics.py --output .local/semantic-eval-new
.\.venv\Scripts\python.exe scripts/verify_semantic_runtime.py --policy .local/semantic-eval-new/candidate-policy.json --output .local/semantic-runtime-new
```

已有 014 数据库升级前先停应用并确认没有待处理请求。维护脚本使用本地管理连接，创建备份，在独立库恢复并校验后应用 015。升级完成后以 off 重启即可沿用原路径。关闭语义检索只需把模式恢复成 off 后重启，原文记录与索引表继续保留。

```powershell
.\.venv\Scripts\python.exe scripts/migrate_conversation_semantics.py --output .local/backups/semantic-upgrade-new
```
