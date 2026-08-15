# 进化与版本治理 Spec

## Problem Statement

看到失败后让模型直接重写整份 Skill，通常会修复一个案例又破坏其他案例。失败也不一定来自 Skill：环境错误、坏题、Judge 漂移和 Simulator 泄漏都可能制造假信号。如果系统没有不可变版本、留出门控和拒绝记录，就无法证明改进来自正确修改，也无法恢复到已知可用状态。

## Solution

建立证据驱动的进化与版本治理流程。系统先按固定顺序排除运行环境、考题和 Judge 问题，再把可信 Skill 失败整理为六类 Failure Card。Updater 只能提出小型 add、update 或 delete Patch，每个操作都引用证据并生成新的不可变候选。Gate 依次执行静态门、触发门、锁定 selection 的 fresh paired evaluation、回归和成本检查。Registry 以 append-only 事件保存候选、接受、拒绝、提升和回滚，任何平局或证据不足都拒绝自动提升。

## User Stories

1. 作为学习者，我想先排除环境、考题和 Judge 问题，以便不把错误归因给 Skill。
2. 作为学习者，我想把可信失败归入固定类别，以便多个 case 能形成可比较的诊断记录。
3. 作为学习者，我想让每张 Failure Card 指向 Trace 和 Judge 证据，以便复核归因。
4. 作为 Updater，我想提出小型结构化补丁，以便修改范围可以审查和测试。
5. 作为学习者，我想在应用前检查补丁目标和前置内容，以便避免错误位置或过期候选。
6. 作为版本系统，我想从接受版本派生不可变候选，以便任何实验都能回到父版本。
7. 作为学习者，我想先跑便宜的静态和触发门，以便明显错误不消耗 selection 预算。
8. 作为学习者，我想在隐藏 selection 集上比较候选和当前接受版本，以便阻止对 develop 过拟合。
9. 作为维护者，我想让回归、成本失控和平局都拒绝候选，以便自动提升保持保守。
10. 作为学习者，我想看到候选被拒的具体 gate 和证据，以便下一次修正有依据。
11. 作为学习者，我想提升通过 gate 的候选并保留旧版本，以便形成完整谱系。
12. 作为学习者，我想回滚到历史接受版本，以便发生退化时快速恢复。
13. 作为评审者，我想区分 Skill 退化和 Judge 协议漂移，以便不误读分数变化。

## Implementation Decisions

- 归因顺序固定为运行与环境、case 与 gold、Judge 与 Simulator、最后才是 Skill。前一层未通过诊断时，不生成 Skill Patch。
- Failure Card 使用六类课程词汇：触发错误、模式错误、问题过载、术语暴露、时机不当、安全越界。
- 每张 Failure Card 记录分类、受影响 case、Trace/Assertion evidence references、观察、归因置信、建议范围和诊断协议版本。
- Updater 只能读取 develop 失败卡片、当前接受 Skill 和允许的 Skill 规范。它不能读取 selection/final case、gold、参考轨迹或逐 case gate 反馈。
- Patch 由一个或多个有序操作组成，每个操作只能是 add、update 或 delete，并记录目标、前置内容 hash、建议内容、证据、理由和风险说明。
- 补丁应用器是确定性的。目标不存在、前置 hash 不匹配、操作冲突或结果不符合 Skill schema 时，整个 Patch 原子失败。
- 每个 candidate 都保存完整可安装内容、父版本、Patch、创建协议和内容 hash。候选不能原地修改父版本。
- Gate 顺序为 candidate validation、Static Gate、Trigger Gate、selection live paired evaluation、回归检查、成本检查和最终决策。
- 便宜 gate 失败后立即停止，不运行后续付费步骤。停止记录明确说明哪些 gate 未执行。
- Selection Gate 使用锁定 6 题，对 current accepted 和 candidate 生成 fresh paired runs。Updater 只收到聚合决策和允许公开的 gate 原因。
- 接受规则必须在配置中版本化。默认保守策略拒绝总体退化、关键 case 回归、触发失败、预算超限、Judge error、证据不足和平局。
- 成本门同时检查绝对预算和相对 accepted 版本的成本增长，阈值必须来自课程实测后锁定。
- GateDecision 保存协议 hash、双方 Skill hash、运行引用、每级 gate 状态、聚合指标、决定和理由。
- Registry 使用 append-only events 构建版本谱系。状态至少区分 candidate、accepted、rejected 和 rolled back；一个实验上下文只有一个 current accepted 指针。
- Promote 只能引用通过完整 gate 的 candidate。Reject 不删除候选。Rollback 创建新事件并切换 accepted 指针，不改写旧事件。
- Judge 或 Simulator 协议变化会建立新实验 lineage，不能把新协议分数直接接到旧进化曲线。
- 课程 fixture 必须可复现至少一次接受，以及一次拒绝或回滚。

## Testing Decisions

- Failure Card 测试使用固定失败证据覆盖六类分类、非 Skill 根因和证据缺失拒绝。
- Updater 权限测试放置 selection/final 诱饵，断言输入和工作区都无法读取。
- Patch parser 和应用器使用表驱动测试覆盖 add/update/delete、冲突、过期 hash、无效目标、原子失败和规范化 hash。
- Candidate 测试证明创建和 gate 不会修改 parent artifact。
- Gate orchestration 测试为每一级注入失败，验证短路顺序、未执行状态和不产生多余付费调用。
- Selection pairing 测试拒绝非 fresh、不同协议、缺 case、iteration 不匹配或隐藏数据泄漏的比较。
- Decision 测试覆盖改进、回归、关键 case 回退、成本超限、平局、Judge error 和预算中断。
- Registry 状态机测试覆盖 candidate、reject、promote、多代 lineage、rollback 和重复命令幂等性。
- 事件重放测试从空状态重建 accepted 指针和谱系，验证历史记录完整。
- CLI 集成测试用 fake engine 完成失败分析、补丁、gate、接受和拒绝两个端到端场景。

## Out of Scope

- 无证据的整份 Skill 自由重写和一次修改多个不相关模块。
- 用 develop 成绩直接提升候选或向 Updater 暴露 selection/final 逐题反馈。
- 自动修改 Judge、Simulator、政策、测试切分或 gate 阈值来让候选通过。
- 删除被拒候选、压平版本历史或覆盖旧报告。
- 在不同模型或评测协议之间宣称严格单调改进。

## Further Notes

- Gate 的目标不是保证候选绝对更好，而是在固定协议和有限样本下阻止已观察到的退化。
- 平局拒绝让自动系统保持保守；学习者仍可以在新实验中修改证据或阈值，但必须显式记录。
- 小补丁与门控是预算、可解释性和回滚能力之间的折中，不是通用最优进化算法。
