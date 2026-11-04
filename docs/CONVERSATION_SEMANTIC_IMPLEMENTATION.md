# 聊天语义索引实施记录

依据：[SPEC](superpowers/specs/2026-11-04-conversation-semantic-index-spec.md)。当前默认off，尚未通过语义质量与人工验收，不宣称已上线。

## S1/S2基础检查点

新增独立profile、作业、片段向量表及事务入队触发器；原文仍追加保存，旧知识混合入口不启用。后台支持补偿扫描、幂等入队、租约接管、最多3次退避重试和原子发布。增加conversations-index backfill/status/retry显式CLI。尚未接入应用生命周期和混合查询。

隔离库新增6项测试通过；会话、摘要与恢复31项回归通过。首次4项失败保留在.local/semantic-s1.log；根因为发布时直接锁授权表需要UPDATE权限，改为不返回正文的受限锁函数，不开放授权表修改。复验见.local/semantic-s1-fixed.log及semantic-s1-regression.log。

S3混合搜索、S4引用/UI、S5真实质量和故障验收继续实施；主库尚未执行015迁移。用户偏好改进不在本次范围。