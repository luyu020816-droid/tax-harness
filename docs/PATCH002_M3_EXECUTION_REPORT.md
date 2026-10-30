# PATCH-002 第3步与第4步部分能力执行报告

实施：2026-10-29；收尾：2026-10-30。目录`D:\tax-harness-remediation`，分支`feature/knowledge-remediation`，独立数据库54338，真实预览8105。

实现提交：`8bbb1415f713bd47abf3974af39f03d1e2627d87`。当前app源码指纹：`4ca57abe7c99ba74165d6f690497af6688fff94be17e914272df79d342c11e2d`。提交未推送远端。

## 结论

第3步的文本知识库闭环已实现：原文件可靠存储、本人/企业隔离、持久索引作业、真实词法与向量检索、持久引用、原文件定位/下载、换版与停用。真实文本PDF已通过HTTP上传、后台处理、真实DeepSeek选择知识工具、页码引用和文件hash校验，未发生发票写入。

第4步的真实重排、单层RRF、企业共享维护、在途版本守卫与RuleBridge也已接通。但**第4步尚未完成**：缺少独立开发/留出集、无答案选择校准及跨片段必要证据扩展，不能宣布召回率达标。历史语义召回、OCR、长上下文与完整交付仍按第5—8步推进。

没有修改main、朋友8104分支、原54337实例或旧项目虚拟环境。没有推送远端、创建PR或合并发布。

## 本批实现

1. 005—007迁移：原文件BYTEA/hash，document/version，blocks/chunks，profile与1024维pgvector，作业、查询、引用和逐企业规则来源。核心ready与知识ready分开；缺vector时不冒充知识可用。
2. PDF/DOCX/MD/TXT的有界解析：真实格式检查、文本页/段/行、表格行与表头/单位定位，独立子进程、超时和内存限制。不以空文本掩盖扫描页缺OCR。
3. 作业独立于聊天worker：阶段、lease、心跳、fencing、原子索引和CAS发布；旧版本继续服务，坏文件和迟到任务不能替换当前完整版本。资料停用同时取消queued/running/blocked任务、增加围栏并释放待处理配额，原文件保留。
4. 固定修订真实BGE-M3、jieba、pgvector及BGE重排，记录权重/词典/停用词/依赖profile。两通道各30，RRF前20重排，最多6组并计证据预算。保存各通道排名、距离、词法分数、RRF和raw logit。
5. 引用冻结真实原文、hash、版本和定位；生成前重验，显示和详情抽屉每5秒重验。旧知识工具来源回读也检查引用，停用内容不能经历史入口重新入模。卡片失效时撤下正文。
6. 知识附件与Excel发票追加分开。资料问答轮次只读，文档里的“自动保存、记住偏好”等文本不能成为办理授权。模型只能选择实际入模引用，不能伪造citation或自由改写业务状态。
7. 企业共享只由维护CLI发布。共享新候选不自动生效，非所有者看不到草稿。RuleBridge验证完整企业范围、日期、当前发布原文、hash及受控执行正文，启用规则的资料不能单独停用或换版。
8. 保留现有暖色布局，增加知识入口、私有上传、查询、真实阶段、引用抽屉、下载、换版/重试/停用。没有重写朋友的整站前端。

接口、状态、失败处理和维护命令见[知识契约](PATCH002_KNOWLEDGE_CONTRACTS.md)。依赖与迁移见[运行说明](PATCH002_KNOWLEDGE_RUNTIME.md)。

## 验证

| 检查 | 已观察结果 | 证据性质 |
|---|---|---|
| 最终后端全量 | **200 passed**，215.56秒 | 2026-10-30 PostgreSQL真实程序测试，含原177项与新增23项，覆盖最后的停用取消修复 |
| 引用呈现调整后的定向回归 | **50 passed**，85.94秒 | 知识、记忆、AnswerRenderer |
| Ruff | app/tests及新增脚本通过；68个后端文件格式通过 | 静态检查 |
| 前端 | TypeScript、Vite生产构建通过 | 编译检查 |
| 固定真实检索组件 | 三条同义问法均由hybrid找出住宿审批条款 | 真实BGE-M3/重排/pgvector；小样例链路，不是留出评测 |
| 真实PDF与模型HTTP | **通过** | 真PDF解析、后台索引、DeepSeek、page=1、原文件hash、跨用户404、0张发票写入 |
| 真实浏览器初轮 | **1条完整流程通过** | 真实上传/检索/DeepSeek、换版、停用、身份隔离 |
| 移动端及引用失效补验 | **通过**，完整真实浏览器流程1.0分钟 | 新会话、本轮引用、390宽度无横向溢出、停用后正文撤下；保留失败脚本记录 |
| 原冻结40例 | 首轮40/40；中间一轮39/40；背景消息修正后 **40/40自动断言通过** | 案例及断言未改，人类签收仍pending；后续停用作业修复另作程序回归 |
| 唯一失败项定向复测 | **3/3通过** | L08-03背景编号与跨会话回忆，真实DeepSeek |
| 备份恢复 | 包含BYTEA、1024维向量、当前指针、持久引用的往返校验通过 | 程序测试用明确标记向量适配器；不计语义质量 |
| 依赖 | pip check通过；Windows/Python3.12锁63个包 | 独立环境，不修改旧项目环境 |

程序测试使用ProgramEngine验证协议、事务和生命周期，代码中明确标注test_only；真实组件、PDF HTTP和本批浏览器用真实本地检索。不能把200项程序通过当成真实模型任务成功率。

现有依赖有Starlette/httpx及jieba/pkg_resources的两项弃用提示，记录保留。没有为消除提示进行无关业务改写。

## 失败记录与修复

| 实际发现 | 直接原因 | 修复 |
|---|---|---|
| 新版成功后检索返回STALE_CONTEXT | 候选SQL未限定active_version_id，旧版也进入候选，最终守卫拒绝 | 两通道与coverage都限定当前发布指针 |
| 恢复校验用例自身失败 | 尝试重开已经close的psycopg连接池 | 按重启行为创建新Database实例复查 |
| 首次PDF取证脚本最后失败 | 按tool_executions不存在的created_at排序 | 改为真实字段tool_call_id，完整重跑，保留原日志 |
| 第一张移动端截图不合格 | 取景落在导航过渡动画且未滚至引用 | 等待导航移出视口并把引用滚入视口 |
| 移动端补验脚本超时 | 对已收起的导航无条件点击“关闭” | 按backdrop实际可见状态处理；不增加超时掩盖定位问题 |
| 引用详情有旧正文副本 | 抽屉原来只读取一次 | 卡片与抽屉复用周期重验，丢弃乱序响应；历史message仅保留定位说明 |
| L08-03在中间全量评测失败 | 模型把用户“不授权修改”混成账号只读，错误理由被程序拒绝后未完成格式修复 | 提示词明确背景消息直接acknowledged/no_changes，read_only只描述权限；三次定向与后续40例均通过 |
| UI补验提前继续上传新版 | 断言匹配旧会话的引用，当前模型轮次尚在运行；换版增加epoch后当前轮被正确拒绝 | 新建会话且只等待最后一轮引用，不把旧卡片算成本轮通过 |
| 停用仍占待处理配额 | queued/running/blocked作业没有随文档停用取消 | 停用同事务取消作业与候选、增加围栏；新增配额释放和迟到worker反例 |

首次失败原始日志、trace和截图保留，没有覆盖为“首次全部通过”。

## 取证位置

- `.local/knowledge-release-200-tests.log`：最终200项回归；之前199项记录继续保留。
- `.local/evidence/patch002-m3-code-binding.json`：提交源码逐文件hash与工作区一致，绑定最终程序日志及各轮真实模型报告。
- `.local/evidence/knowledge-real-components.json`：真实模型profile、查询、证据和计时。
- `.local/evidence/knowledge-real-pdf-http.json`：完整真实PDF/模型/原文件/权限取证。
- `.local/evidence/patch002-knowledge-ui/`：桌面与移动端截图。
- `.local/evidence/patch002-knowledge-ui-mobile-script-failure/`：补验脚本失败截图与trace。
- `.local/knowledge-browser-release.log`：最后一次浏览器完整通过结果；verified.log保留提前匹配旧轮次的失败。
- `.local/evidence/patch002-knowledge-ui-old-turn-assertion-failure/`：旧轮次断言失败的截图与trace。
- `.local/evidence/patch002-knowledge-frozen-40/report.json`：首轮40例；源码指纹c944bc56564dff93be1f7b69f6443b659c976d4dd4db23c8b0bbbf19777c0c88。
- `.local/evidence/patch002-knowledge-final-frozen-40/report.json`：中间39/40的完整失败记录，未改写。
- `.local/evidence/patch002-knowledge-background-fixed/report.json`：背景消息三次定向复测。
- `.local/evidence/patch002-knowledge-release-frozen-40/report.json`：修正后的40/40，内容指纹9415ae349dfe777203f207b3874c1a3b8687d22360e8761b25cd00393d269f95。

运行中源码尚未提交，report的当时HEAD是本批之前的提交。核对以report里的实际内容fingerprint为准，不能冒称当时已经运行在后续提交上。最后的停用作业取消发生在该40例之后，改动限于knowledge.py和knowledge_admin.py的取消逻辑，并由新增程序反例验证；未把原40例报告指纹冒充当前整树指纹。取证目录被Git忽略；只有明确选择和脱敏后才纳入比赛交付。

## 下一项

按第4步建立文档级互斥的开发集与留出集，覆盖同义表达、否定/例外、表格/单位、精确票号、旧版本、权限和无答案。测candidate Recall@20、必要证据集合覆盖、引用支持与降级表现，再校准证据选择/无答案规则及跨chunk扩展。当前status=ok只说明检索范围和索引正常，不代表答案已得到充分支持。

不增加第二套历史框架；第4步验证后，第5步将既有原始Source接入同一检索profile/失效机制。扫描PDF当前明确blocked/OCR_REQUIRED，不宣称已支持OCR或任意长历史零遗漏。
