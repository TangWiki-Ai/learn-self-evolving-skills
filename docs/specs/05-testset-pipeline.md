# 测试集流水线 Spec

## Problem Statement

自进化系统会优化它反复看到的指标。如果题目来自随手编写、gold 由人手填、Judge 没有先验证，系统只会学会迎合有缺陷的考卷。另一方面，直接把对话语料当作可执行 case 也行不通：ABCD 提供表达和意图，但没有课程环境终态；tau2-bench 提供运行难度信号，但不属于本课程可重放环境。课程需要把“考什么”和“怎么判”分开，再用可追溯流程合成可靠题目。

## Solution

实现 Scrub、Cluster、Stratify、Verify、Calibrate、Split 六阶段流水线。ABCD 提供退货退款表达和意图分布，tau2-bench 提供重复轨迹与通过率难度信号，STATE-Bench shop 环境提供可执行任务和确定性政策 oracle。流水线先产出候选，再在受控政策维度上生成变体，通过环境重放、Judge 正反试判和人工抽读后，只把合格题加入 develop。selection 和 final 从项目开始锁定，不接收后续生成题。

## User Stories

1. 作为学习者，我想获取固定版本的三个上游数据源，以便每次课程处理同一批输入。
2. 作为学习者，我想清洗并去重 ABCD 退货退款语料，以便聚类不被重复对话主导。
3. 作为学习者，我想保留原文与 delexed 版本关系，以便检查清洗是否改变语义。
4. 作为学习者，我想把聚类结果与 flow/subflow 标签比较，以便评估意图发现质量。
5. 作为学习者，我想保留低频高风险意图，以便长尾案例不会被当成噪声删除。
6. 作为学习者，我想从 tau2-bench 的同任务多次运行中学习去重，以便不把同一任务当成多道独立题。
7. 作为学习者，我想按历史通过率分层采样，以便候选集同时包含简单、中等和困难任务。
8. 作为学习者，我想修改会员等级、退货窗口和促销组合生成变体，以便扩充可执行 develop 集。
9. 作为学习者，我想让政策引擎自动计算 gold，以便答案与环境规则一致。
10. 作为学习者，我想让一份正确答卷和一份故意错误答卷都通过预期 Judge 检查，以便证明考卷有区分力。
11. 作为学习者，我想记录人工抽读结论，以便自动检查无法覆盖的语义问题有审计记录。
12. 作为系统维护者，我想强制所有 split 互斥，以便 Creator、Updater 和 final 不发生数据泄漏。
13. 作为评审者，我想查看每道新题的来源和转换谱系，以便确认它不是无法追溯的 LLM 生成内容。

## Implementation Decisions

- STATE-Bench `customer_support` 固定 commit `5644b183`，为所有 live evaluation 提供可执行环境和任务语义。
- ABCD 课程切片包含 PRD 指定的 product defect 等退货退款子集，共 1,070 段候选语料，并保留原文、delexed 文本和 flow/subflow 标签。
- tau2-bench retail 切片只读，用 114 个任务的多次运行轨迹学习语义去重和按通过率分层；它不进入 shop 环境重放。
- 所有公开文档把上游材料描述为 benchmark 或角色扮演数据，不称为真实生产日志。
- 每份上游数据都有固定版本、原许可证、下载来源、checksum、切片条件和转换工具版本。
- Scrub 阶段规范文本、去除重复和无效记录、验证脱敏关系，并生成稳定 source record ID。它不重写对话意图。
- Cluster 阶段使用给定的 embedding 和聚类封装，输出每条记录的 cluster、置信信息和代表样本，并用现有标签做外部自评。
- Stratify 阶段综合意图覆盖、长尾风险、语义去重和 tau2 难度信号，产出候选清单而不是直接写入测试集。
- Verify 先从结构化字段和对话内容提取低成本信号，再让锁定的小模型用严格 JSON 判断意图、失败类型、证据片段和环境可映射性。确定性能力门和模型判断都通过，候选才进入变体生成。无法映射到可执行环境的候选保留审计记录，但不能成为可评分 case。
- Variant generator 只改变 schema 允许的政策维度，生成公开用户 intent、环境 seed 和 oracle 输入，不直接生成文本答案。
- LLM 可以起草公开表达和依赖 `tool_timeline`、`key_messages` 的语义 rubric，但不能起草金额、终态或政策 gold。模型草案必须显示在人工审核 packet 中，并在人工明确激活前保持 advisory 状态。
- Gold 只能由同版本政策引擎计算。每个变体保存 oracle 输入、政策版本和结果 hash。
- Calibrate 执行三步“考卷先考自己”：标准操作环境重放对账、Judge 对故意正确和错误答卷的试判、人工抽读与结论记录。
- 只有三步都通过的 case 才获得 qualified 状态。失败 case 保存原因，可修复后作为新版本重新验证。
- 初始 split 维持 PRD 数量：creator 9、develop 6、selection 6、final 12、trigger-eval 20。课程只允许把合格新题加入 develop，使其扩展到至少 15。
- selection 和 final 的 case ID、内容与协议在课程开始前锁定。任何修改都创建新的课程数据版本并使旧参考结果不可直接比较。
- Creator 只能读取 creator 成功轨迹；Updater 只能读取 develop 失败证据；Agent 只读取当前消息、当前 Skill 和工具结果。
- 数据目录提供机器可读 manifest，记录 split、可见角色、source lineage、schema、checksum、qualification 和锁定状态。
- 默认 CI 回放签入的固定模型响应；显式 live 模式才通过 ClaudeCLI 调用锁定 Provider，并记录模型、prompt、响应 hash、token、耗时和调用来源。两种模式共用同一解析器、schema 和确定性门。
- LLM 可以辅助筛选、表达和 rubric 起草，但其产物必须经过相同的 oracle、重放、Judge 和人工流程，不能凭生成来源直接入库。

## Testing Decisions

- 获取测试在固定小样本上验证版本、checksum、license 和转换可复现，不在默认 CI 下载完整数据。
- Scrub 测试覆盖重复、空对话、脱敏映射损坏、编码、稳定 ID 和不改变意图的规范化。
- Cluster 测试关注输出契约、稳定随机种子、标签对照指标和长尾保留，不锁定特定库的内部聚类顺序。
- Stratify 测试验证同任务去重、难度桶边界、意图配额和少数高风险类别不会被全部过滤。
- Variant 测试对每个政策维度做表驱动组合，并验证无效组合被明确拒绝。
- Oracle 重放测试要求标准操作终态与政策结果完全一致。
- Judge calibration 测试至少包含可通过、应失败和证据不足答卷，验证题目能区分这些状态。
- Split 测试检查内容 hash 和语义来源两层互斥，禁止新题写入 selection、final 或 creator。
- 权限测试从 Creator、Updater、Agent 和报告的视角读取数据，断言各自只能看到允许字段。
- CLI 集成测试从候选记录运行到 qualified develop case，并验证失败阶段保留可读审计记录。
- Curation 测试验证模型必须引用真实 source turn、确定性能力门可以否决模型误判、固定模式不读取 Key，以及 live 模式必须显式开启。

## Out of Scope

- 把 ABCD 或 tau2-bench 对话直接当作 STATE-Bench 可执行任务。
- 自动扩充 selection 或 final，或让进化 Agent读取其答案和失败细节。
- 依赖无法固定版本的在线搜索或私有生产日志。
- 用人工手填退款终态、金额或政策判断。
- 构建面向所有客服领域的通用 benchmark 生成平台。

## Further Notes

- 数据流水线首先保证可信与可追溯，再追求规模。develop 达到 15+ 是课程目标，不是越多越好。
- “真实”只描述真人扮演语料的语言特征；课程报告必须如实说明环境和轨迹来自 benchmark。
- selection/final 隔离是自进化可信度的基础，不能为了调试方便增加隐藏开关。
