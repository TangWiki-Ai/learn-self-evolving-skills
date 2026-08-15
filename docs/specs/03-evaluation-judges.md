# 评测与 Judges Spec

## Problem Statement

Agent 的最终回复可能承诺已经退款，但环境没有发生任何变化；也可能得到正确终态，却绕过必要确认或调用了危险工具。只读 transcript 无法可靠判定这些情况。反过来，把所有判断交给 LLM Judge 又会引入位置、长度、自偏好和随机性偏差。课程需要一套证据优先、成本分层、可校准且能解释每个结论的评测模型。

## Solution

把 Claude Code stream-json 解析为不可变 Trace，并从订单环境生成 StateDiff 和确定性证据。评测按成本和可靠性从 `expect`、State Judge、Rule Judge 到 LLM Judge、Agent Judge 逐层执行。脚本能确定的事实永远由脚本计算；模型只判断需要语义理解的断言。所有 Judge 输出统一的 assertion-level grading record，包含状态、证据、rubric 版本和未评估原因。

## User Stories

1. 作为学习者，我想把原始流式事件解析成统一 Trace，以便不同 Judge 读取同一种数据。
2. 作为学习者，我想在调用模型前检查显然无效的 case，以便节省费用并得到清晰错误。
3. 作为学习者，我想比较预期终态和实际 StateDiff，以便验证业务操作是否发生。
4. 作为学习者，我想断言某个工具被调用或按指定顺序调用，以便验证安全流程。
5. 作为学习者，我想让基础失败优先于派生通过，以便最终结果不会掩盖关键错误。
6. 作为学习者，我想让 Judge 返回 not evaluated，以便证据不足时不伪造结论。
7. 作为学习者，我想查看每条断言引用的消息、工具事件或状态字段，以便复核判分。
8. 作为学习者，我想比较 rubric LLM Judge 和 evidence Agent Judge，以便理解两种方法的适用边界。
9. 作为 Judge agent，我想读取脚本预先抽取的事实，以便把推理集中在语义判断上。
10. 作为课程维护者，我想锁定 rubric 和 Judge 模型版本，以便历史分数可以解释。
11. 作为测试作者，我想用固定 Trace 测试 Judge，以便默认 CI 不调用真实模型。
12. 作为评审者，我想区分任务失败、Judge 错误和未评估，以便报告不会把基础设施故障算成能力失败。

## Implementation Decisions

- Trace 是跨模块的不可变记录，包含 run、case、iteration、session、消息、工具请求与结果、时间线、退出状态、用量、成本、Skill hash 和版本信息。
- Trace parser 保留原始事件顺序和稳定事件标识。无法识别的非关键事件可以保留为扩展记录；关键事件畸形时，parser 返回结构化失败而不是猜测。
- 时间、金额和 token 使用规范类型。原始文本可以保留用于审计，但 Judge 读取经过验证的结构字段。
- `expect` 在启动 Agent 前验证 case schema、fixture、必要工具、预算和环境前置条件。前置失败不产生模型费用。
- State Judge 比较环境前后 snapshot 和 case 的确定性预期，逐项说明实际值、预期值和对应 StateDiff。
- Rule Judge 支持工具是否调用、调用次数、参数约束、先后顺序、禁止调用和 transcript 结构等断言。
- Judge 聚合采用 failure-first 规则：任何必要断言失败都会阻止总体通过；error 和 not evaluated 与 fail 分开保存。
- LLM Judge 只处理脚本难以确定的语义质量。它一次读取规范化 rubric、允许的 transcript 片段和证据，输出经过 schema 校验的 grading record。
- LLM Judge 必须提供 not evaluated 出口。输出缺字段、证据引用无效或无法解析时记为 Judge error，不重试成随机答案。
- Agent Judge 先由确定性 evidence extractor 生成 StateDiff、工具时间线、金额对账和关键消息索引，再让只读 Judge agent 逐断言判定。
- Judge agent 不能访问 shop 写工具、Skill 源码修改工具、selection/final 私有答案或参考轨迹。
- 每条 AssertionResult 包含 assertion 标识、判定状态、简短理由、结构化 evidence references、Judge 类型和版本。
- 总体 CaseGrade 保存各 Judge 的独立结果和聚合结果，不覆盖分歧。报告可以比较 Judge 与人工标签。
- Rubric、证据提取版本、Judge 模型和关键提示都记录 hash。修改任一项会创建新的评测协议版本。
- 人工校准集与课程测试数据分开管理。课程只能报告实际测得的一致率，不能把 PRD 中的目标数字当成结果。

## Testing Decisions

- Trace parser 使用真实形状的 stream-json fixtures 覆盖文本分块、并行工具事件、resume、错误、截断和未知事件。
- State Judge 使用表驱动案例覆盖完全通过、缺失变化、多余变化、金额错误和环境错误。
- Rule Judge 使用事件时间线测试调用存在、次数、参数、顺序、禁止调用和 failure-first 聚合。
- 所有 Judge 共享合约测试，验证 pass、fail、not evaluated、error 和 evidence reference 的 schema。
- LLM/Agent Judge 默认使用固定模型响应和失败响应，测试 parser 与校验行为，不测试模型内部推理。
- Evidence extractor 使用黄金 Trace 夹具验证金额对账、工具顺序和状态事实，输出排序必须稳定。
- 防权限测试证明 Judge agent 只能看到允许的证据，并且调用写工具会被环境拒绝。
- 校准实验作为显式课程任务运行，固定输入顺序策略并保存人工标签、模型输出和差异摘要。
- CLI 集成测试从单 case 运行到 grading record，断言基础设施 error 不会被计入 Agent fail。

## Out of Scope

- 用单一 LLM 分数替代确定性状态与规则判断。
- 自动把 Judge 分歧写回 rubric 或在 final 运行期间修改评测协议。
- 对模型主观风格做通用人类偏好排名。
- 在没有人工标签的情况下宣称 Judge 已校准。
- 让学生在课程主线中实现底层 stream transport 或模型 SDK。

## Further Notes

- State Judge 是业务终态的最高可信证据，但不能替代工具顺序和对话安全规则；不同 Judge 解决不同问题。
- Agent Judge 的价值来自先抽取事实再判断，而不是增加更多自由推理步骤。
- 评测协议一旦用于 selection 或 final，就必须锁定；任何变更都应视为新实验。
