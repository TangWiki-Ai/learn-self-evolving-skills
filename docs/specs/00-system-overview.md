# 系统总览 Spec

## Problem Statement

会写 Agent Skill 的开发者通常只能凭几段对话判断 Skill 是否变好。他们缺少可执行环境、可靠判分、可追溯测试集、隔离的留出集和版本门控，因此无法证明一次修改真的提升能力，也无法在退化时安全回滚。现有评测框架可以代跑测试，但会替学生隐藏最关键的判断逻辑，无法达到课程要求的工程深度。

课程还面对三个现实约束：国内模型端点的兼容性不确定；数据必须诚实、可重放并保持来源；完整实验必须控制在个人可承担的预算内。系统需要让学生亲手实现核心机制，同时提供足够稳定的电商环境和安全隔离，避免课程时间消耗在无关基础设施上。

## Solution

构建一个独立的 Python 课程仓库和 `ses` CLI。系统使用 STATE-Bench 的退货退款环境执行 fresh rollout，使用 ABCD 和 tau2-bench 构造测试题来源，通过确定性状态证据、规则和校准后的模型 Judge 评估结果。学生从成功轨迹创建 Skill v0，再根据失败证据生成小型结构化补丁。selection gate 只接受有证据的单调改进，版本注册表保存接受、拒绝和回滚记录。最后，有界自动循环完成至少两轮进化，并在锁定 final 集上给出一次独立验证和可分享的作品集。

系统将环境仿真与安全隔离作为给定脚手架，将测试集判断、评测、进化和门控逻辑留给学生实现。每课必须输出一个可复现的 before/after 或 with/without 结果。

## User Stories

1. 作为学习者，我想先运行一个快速诊断，以便在写课程代码前知道数据、模型和 MCP 是否可用。
2. 作为学习者，我想在同一个退货案例上比较无 Skill 和有 Skill 的 fresh rollout，以便直观看见 Skill 的影响。
3. 作为学习者，我想从 Claude Code 的流式输出构造统一 Trace，以便后续 Judge 不依赖原始终端文本。
4. 作为学习者，我想比较订单执行前后的状态，以便判断 Agent 是否真正完成业务操作。
5. 作为学习者，我想用规则验证关键工具和调用顺序，以便识别“说了但没做”的回答。
6. 作为学习者，我想校准 LLM Judge 和证据式 Agent Judge，以便知道何时可以相信模型判分。
7. 作为学习者，我想批量运行 develop 案例并重复抽样，以便观察成功率、成本和方差。
8. 作为学习者，我想从 ABCD 和 tau2-bench 提炼题目候选，以便测试集覆盖真实表达和不同难度。
9. 作为学习者，我想让确定性政策引擎生成变体题的 gold，以便避免人工填写错误答案。
10. 作为学习者，我想在入库前验证考题、Judge 和人工判断，以便先证明考卷本身可信。
11. 作为学习者，我想从审核过的成功轨迹生成 Skill v0，以便把可复用行为提炼成说明书。
12. 作为学习者，我想测试 Skill 的触发 precision 和 recall，以便发现误触发与漏触发。
13. 作为学习者，我想把失败归类并链接到具体证据，以便补丁针对真实问题。
14. 作为学习者，我想只应用小型 add、update 或 delete 补丁，以便控制修改范围。
15. 作为学习者，我想在锁定 selection 集上比较候选与已接受版本，以便阻止回归进入主线。
16. 作为学习者，我想查看 Skill 的完整版本谱系，以便理解接受、拒绝和回滚过程。
17. 作为学习者，我想在预算和轮数护栏内自动运行进化循环，以便观察系统如何在可靠信号下自治。
18. 作为学习者，我想只在结束时运行 final 集，以便获得没有参与调参的提升证据。
19. 作为学习者，我想导出单文件报告和作品集，以便展示系统设计、版本记录和实验结果。
20. 作为课程维护者，我想让每课 solution 成为下一课 starter，以便所有学生从已验证状态继续。
21. 作为课程维护者，我想锁定数据版本、模型配置和参考运行，以便复现课程结果。
22. 作为课程维护者，我想在默认 CI 中使用 fake engine，以便测试不会产生费用或泄漏密钥。
23. 作为评审者，我想追溯每个分数、补丁和 gate 决策的证据，以便判断作品集是否可信。
24. 作为未来维护者，我想在不改 evaluator 的前提下添加 Provider，以便扩展模型来源而不重写课程核心。

## Implementation Decisions

- 系统作为独立 Python 3.11+ 项目实现，使用 Pydantic v2 表达跨模块记录，使用 pytest 验证行为，不引入重型 Agent 框架。
- `skill-up` 只提供机制参考。课程不导入其私有实现，也不依赖其 CLI 执行。
- `ses` CLI 是用户入口。业务逻辑放在独立模块中，CLI 只解析命令、调用用例并呈现结果。
- Foundation Runtime 负责配置、模型锁、凭据读取、数据目录、Engine 调用和 case 工作区隔离。
- Shop Environment 负责订单状态、政策 oracle、工具和 MCP server。每次 case 都从已知种子创建独立环境。
- Evaluation & Judges 负责 Trace、StateDiff、断言和判定记录。确定性证据优先于模型判断。
- Simulation/Runner/Reporting 负责多轮对话、批量执行、恢复、预算、运行记录和 L1/L2/L3 HTML。
- Testset Pipeline 负责从上游语料到 develop 题目的全过程，并强制 creator、develop、selection、final 的可见性规则。
- Skill Creation 负责安全安装、Creator 隔离、静态检查、触发评测和 v0 对照。
- Evolution & Governance 负责失败卡片、结构化补丁、selection gate、不可变版本和回滚。
- Automation & Portfolio 只编排已有能力，不复制 Judge、gate 或 registry 逻辑。
- 首版只实现 Claude Code headless + 硅基流动。Engine 合约只暴露运行请求、流式事件、用量和结束状态；Provider 特有配置停留在适配器内部。
- 主 Agent 与 Creator 使用 DeepSeek 系模型；Simulator 与 Judge 使用 Qwen 系低成本模型。实际模型标识由锁文件固定，不写死在业务逻辑中。
- 系统把运行、Trace、Skill 版本、补丁和 gate 决策视为不可变记录。新实验产生新标识，不覆盖历史结果。
- 跨模块记录采用 producer-owned contract。消费者导入 canonical schema，不复制相似模型；持久化记录遵守 `v1alpha1` 版本、规范 JSON、相对 artifact reference 和稳定 hash 规则。
- 所有 modifying agent 与 Creator 都不能读取 selection/final 题面、gold、参考轨迹、逐题反馈
  或 Judge 私有材料。只有受信 runner 在执行对应 slot 时，才向被测 Agent 提供该题公开请求。
- 报告只展示允许给当前角色看的证据。公开作品集不能包含凭据、隐藏答案或可反推 final 的私有数据。
- 预算护栏覆盖 case 数、轮数、token 和费用。中断保留完整的已完成记录和标记清楚的部分记录。
- 数据来源严格按 PRD 分工：STATE-Bench 执行；ABCD 提供语言和意图；tau2-bench 提供去重与难度信号。
- 项目只提交固定课程切片和来源材料，通过可复现脚本获取完整上游数据。
- 课程总预算目标为不超过人民币 50 元。估算和实测必须分开标注。

## Testing Decisions

- 主要测试从 CLI 发起，在临时工作区验证输入、隔离、领域行为、结构化产物和报告。测试只断言外部行为，不锁定内部函数调用顺序。
- fake engine 必须重放真实形状的流式事件、工具调用、错误和用量数据，让默认测试覆盖完整主路径。
- 真实 Provider 测试使用显式 smoke 标记，默认跳过，并且永远不在无人工授权的 CI 中运行。
- 政策 oracle、StateDiff、规则优先级、切分互斥、补丁应用和 gate 决策使用表驱动单元测试覆盖边界组合。
- 每个跨模块 Pydantic 记录都要做生产者到消费者的契约测试，包括向后兼容的读取行为。
- Contract 测试同时验证未知字段、版本、金额、时间、路径和脱敏不变量；接口变更先更新跨模块契约 spec。
- 防泄漏测试从 Agent 工作区和公开报告视角检查文件、字段和提示上下文，而不只检查配置值。
- 课程测试验证 starter 会失败于本课缺失能力，solution 会通过，并验证上一课 solution 与下一课 starter 一致。
- 报告测试同时验证语义数据和渲染结果，避免只做脆弱的整页字符串快照。
- 最终 clean-room 验证从全新环境完成 10 课，记录命令、结果、费用与任何偏差。

## Out of Scope

- 通用 Agent 托管平台、Web SaaS、多人账户、远程队列和云端控制面。
- 首版多 Provider 实现、自动 Provider 选择和跨 Provider 结果等价保证。
- 使用真实企业生产日志或把 benchmark 数据包装成生产数据。
- 训练或微调基础模型、修改 Claude Code 本体、实现通用 Agent 框架。
- 让自动循环修改课程源码、Judge、测试协议、selection 或 final 数据。
- 用单次 final 成绩作为持续调参反馈。
- 为所有电商客服意图建立完整生产系统；首版聚焦退货退款。

## Further Notes

- Phase 0 是快速 Go/No-Go 冒烟，不是稳定性研究。更大样本的 tool calling、成本和方差测量进入对应课程 ticket。
- 每个功能 ticket 必须同时交付可运行行为、测试、对应课程材料和对照产物。
- 如果实测否定硅基流动主路径，团队应记录证据后再决定是否引入路由兜底，不能提前扩大首版范围。
- 所有公开表述都应使用“生产式业务流程”“benchmark 数据”或“角色扮演语料”。
