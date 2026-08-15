# 自动进化与作品集 Spec

## Problem Statement

手动执行 rollout、归因、补丁和 gate 可以证明单次流程，但无法展示系统如何在多个候选之间持续保持边界。一个没有停止条件的自改循环又会迅速消耗预算、围绕噪声震荡，甚至通过修改评测协议获得虚假提升。课程需要自动化已有可靠步骤，同时严格限制循环能改什么、花多少钱、何时冻结，以及 final 何时可以运行。

## Solution

提供一个有界 Auto-Evolve Orchestrator。它只调用已验证的 Runner、Failure Analyzer、Updater、Gate 和 Registry，按 rollout、reflect、patch、gate 循环，不复制这些模块的判断逻辑。循环受最大轮数、token、费用、冷却期和改进冻结阈值控制，课程运行至少完成两轮。结束后，系统对 current accepted Skill 执行锁定的 12 题 final，并导出版本、拒绝分支、gate 记录、L3 报告、架构说明和一页项目总结。

## User Stories

1. 作为学习者，我想用一个命令启动有界进化，以便观察完整循环而不手动串联每步。
2. 作为学习者，我想设置最大轮数和预算，以便实验不会无限运行。
3. 作为学习者，我想让每轮只从当前 accepted 版本派生一个候选，以便谱系清楚。
4. 作为学习者，我想让被拒候选进入冷却或冻结逻辑，以便系统不会重复同类无效修改。
5. 作为学习者，我想在改进低于阈值时停止，以便避免围绕噪声持续震荡。
6. 作为学习者，我想在中断后查看已完成轮次并安全恢复，以便不重复花费。
7. 作为学习者，我想让手动和自动候选经过同一个 Gate，以便自动模式没有后门。
8. 作为学习者，我想在循环结束后运行一次 final，以便得到未参与修改的独立成绩。
9. 作为维护者，我想在 final 后修改 Skill 时创建新实验声明，以便成绩不会被继续调参污染。
10. 作为学习者，我想查看 L3 进化曲线和拒绝分支，以便理解分数与成本如何变化。
11. 作为学习者，我想导出可离线分享的 portfolio，以便展示系统、证据和最终结果。
12. 作为无 Key 的读者，我想查看官方参考 portfolio，以便理解成果结构。
13. 作为安全评审者，我想证明自动循环不能修改 Judge、数据切分或 gate 协议，以便自治范围可控。

## Implementation Decisions

- Auto-Evolve 是应用层 orchestrator，只组合 Runner、Failure Analyzer、Updater、Gate 和 Registry 的公开接口。
- 每轮从 current accepted Skill 开始，在 develop 上产生 fresh rollout 或读取当前轮明确绑定的 fresh run，生成失败卡片和一个候选 Patch。
- 每个 candidate 都走与手动流程相同的 validation 和 Gate。自动模式不能降低阈值、跳过 selection 或直接 promote。
- LoopState 保存实验标识、配置 hash、当前轮、accepted 版本、候选、预算消耗、停止原因和所有步骤引用。
- 护栏至少包括最大轮数、总 token、总费用、连续拒绝数、补丁类别冷却和最小改进冻结阈值。
- 预算或基础设施中断保留当前步骤的部分产物，但只有完整 GateDecision 才能改变 accepted 指针。
- 冷却策略基于失败类别和 Patch 目标，防止连续轮次重复相同无效修改；它不能屏蔽新出现的安全失败。
- 冻结策略在可比较 gate 指标连续低于最小改进或无合格失败证据时停止。停止不是成功，也不能伪造提升。
- 课程验收要求自动循环在预算内完成至少两轮进化。轮数包含被 gate 接受或拒绝的完整候选轮次。
- Final split 固定 12 题，只对循环结束时的 current accepted Skill 执行，并且不能向 Creator、Updater 或自动循环返回逐题修改信号。
- Final run 使用锁定的 Engine、Simulator、Judge 和报告协议。协议改变后必须声明新实验，旧 final 与新结果不能拼接。
- Final 一次性原则通过 manifest 和用户提示执行：final 后发生 Skill 修改时，系统标记旧结果属于前一实验，并要求重新声明新实验。
- L3 report 展示完整版本 DAG、接受与拒绝分支、每轮 develop/selection 指标、成本、GateDecision 和 final 汇总。
- Portfolio 收集确切 Skill artifacts、registry events、gate records、final report、进化曲线、架构图和一页系统说明。
- Portfolio 使用 allowlist 导出，不包含环境凭据、hidden gold、selection/final 私有 case、完整内部路径或可反推出答案的数据。
- 官方参考 portfolio 标记模型、数据、协议、日期和实测成本，不能冒充学习者自己的 fresh run。

## Testing Decisions

- Orchestrator 使用 fake Runner、Analyzer、Updater、Gate 和 Registry 测试步骤顺序与传递契约，不重复测试各模块内部规则。
- 状态机测试覆盖候选接受、候选拒绝、无失败证据、连续拒绝、冷却、冻结、预算停止、异常和恢复。
- 幂等测试重复执行已完成步骤，断言不会重复调用付费动作或追加冲突 registry event。
- 预算测试在每个步骤前后触发上限，验证只有完整 gate 可以改变 accepted。
- 权限测试向自动工作区放置 Judge、selection 和 final 诱饵，断言 orchestrator 和 Updater 不可见。
- Final 测试证明循环期间无法选择 final，并且 final 输出不会成为下一轮输入。
- L3 数据测试验证版本 DAG、拒绝分支、指标协议、成本双轴数据和 final 关联。
- Portfolio 测试从完整 fake 实验导出后重新打开所有引用，扫描秘密和隐藏数据，并验证架构说明与 manifest 一致。
- 课程验收 fixture 至少产生两轮、一次接受和一次拒绝或回滚，结果可在无网络测试中复现。

## Out of Scope

- 无限在线学习、后台常驻自改、生产流量接入和无人监管的模型自治。
- 自动修改源码、测试、Judge、Simulator、政策、数据切分、预算或 Gate 规则。
- 并行搜索大量候选、群体优化和跨 Provider 比赛。
- 用 final 逐题结果继续生成 Patch。
- 把 portfolio 发布到外部平台或自动生成未经审查的简历声明。

## Further Notes

- 自动化只在判分信号足够可靠时才有意义。任何 Judge 或数据完整性问题都应先停止循环。
- L3 曲线必须同时展示能力和成本，防止用不可接受的费用换取微小分数提升。
- Portfolio 是可审计实验包，不是只展示最佳数字的宣传页面。
