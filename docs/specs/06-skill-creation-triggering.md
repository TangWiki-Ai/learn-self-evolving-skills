# Skill 创建与触发 Spec

## Problem Statement

从几条成功对话生成一份长提示词并不等于得到可靠 Skill。Creator 可能复制订单 ID、固定答案或失败模式，生成的 description 也可能漏触发或误触发。即使 develop 得分上升，如果对照运行不新鲜、Skill 安装混入 eval 文件，结果仍然不可信。系统需要约束 Skill 的输入、结构、安装和触发行为，再用 paired live evaluation 证明 v0 的实际影响。

## Solution

提供安全 Skill 安装器、隔离 Creator Adapter、Static Gate、Trigger Evaluator 和 v0 对照流程。Creator 只读取 9 条三重复核的成功轨迹，并且只能修改候选 Skill 工作区。Static Gate 检查工具白名单、固定答案、案例标识和内容规模；Trigger Evaluator 在 10 条正向与 10 条负向 prompt 上测 precision/recall。通过结构和触发门后，v0 与 baseline 在 develop 上运行 fresh paired comparison，并输出 L2 报告。

## User Stories

1. 作为初学者，我想用课程提示词生成第一个 demo Skill，以便先看到 Skill 对同一案例的影响。
2. 作为初学者，我想在生成质量不足时使用参考 Skill，以便课程不会卡在第一课。
3. 作为 evaluator，我想只安装 Skill 正文和引用材料，以便 eval 数据不会进入 Agent 上下文。
4. 作为 Creator，我想读取审核过的成功轨迹，以便从可复用行为中归纳 Skill。
5. 作为安全评审者，我想限制 Creator 可用工具，以便它不能读取留出集或修改课程代码。
6. 作为学习者，我想在运行模型前发现订单 ID、固定答案和不支持工具，以便避免浪费评测费用。
7. 作为学习者，我想测试正向 prompt 是否触发 Skill，以便发现 description 漏触发。
8. 作为学习者，我想测试负向 prompt 不触发 Skill，以便发现 Skill 污染其他任务。
9. 作为学习者，我想比较 v0 与 baseline 的同题 fresh runs，以便看到哪些 case 改进或回退。
10. 作为版本系统，我想用内容 hash 标识 Skill，以便 Trace、补丁和报告引用确切版本。
11. 作为维护者，我想替换 Creator 实现而不改变后续 gate，以便生成方法可以独立演进。

## Implementation Decisions

- Skill artifact 包含 SKILL.md、允许的 references、manifest、内容 hash 和来源版本。eval、运行轨迹、gold 和隐藏材料永远不属于可安装内容。
- 安装器使用 include allowlist 复制到 Claude Code 约定的 Skill 位置，并在运行前验证实际安装清单。
- 每次 baseline 和 with-Skill 运行都创建新工作区、新环境和新 Trace。卸载 Skill 后的缓存结果不能充当 baseline。
- 第一课 demo 流程允许学习者用给定 Creator prompt 生成 Skill；参考 Skill 只在生成结果不可用时提供明确标记的兜底。
- Creator Adapter 接收经过审核的 seed traces 和生成约束，返回候选 Skill artifact。后续模块不依赖 Creator 的具体模型或提示实现。
- v0 的 seed 只包含 creator split 的 9 条成功轨迹。每条轨迹必须通过 State Judge 和模型 Judge。课程 fixed/offline 可以读取绑定完整证据链且明确标为待人工复核的 attestation；live Creator 必须读取独立签名的人审结论，否则关闭。
- Creator 在独立工作区运行，只能读取安全 seed projection、Skill 规范和安全工具。它看不到完整 seed trace、develop 失败、selection、final、参考答案或整个项目源码。
- Static Gate 在任何 live evaluation 前执行，检查必需元数据、工具白名单、长度限制、危险指令、订单或顾客标识、case 特有答案和 eval 内容。
- Static Gate 返回逐条结构化结果。失败候选保留审计记录，但不能安装或进入 Trigger Evaluator。
- Trigger Evaluator 使用 10 条正向与 10 条负向 prompt，观察 Claude Code 原生 Skill 发现行为，不用自建关键词路由替代产品行为。
- Trigger result 记录每条 prompt 的预期、实际触发、证据、Skill hash 和引擎版本，并计算 precision、recall 与混淆矩阵。
- v0 comparison 使用相同 develop case、iteration 策略、模型锁、Simulator 和 Judge 协议。两边都产生新的线上轨迹。
- L2 comparison 保留 case-level pair，区分 pass-to-fail、fail-to-pass、共同通过和共同失败，并同时展示成本变化。
- 通过 v0 流程不等于自动接受后续版本。v0 成为初始已接受基线，后续版本必须经过 Evolution Gate。
- Skill manifest 为未来其他 Provider 预留兼容性声明，但首版只验证 Claude Code 原生发现机制。

## Testing Decisions

- 安装器测试从 artifact 到 Agent 工作区检查精确文件清单，并用诱饵 eval 和 gold 文件证明 allowlist 有效。
- Static Gate 使用表驱动 fixtures 覆盖合法 Skill、缺失元数据、未知工具、固定答案、case ID、危险指令和长度边界。
- Creator Adapter 默认使用 fake engine，测试输入可见性、工具 allowlist、输出校验和失败保留。
- Seed manifest 测试要求恰好引用 9 条完整验证的 creator 轨迹，拒绝 develop、失败 Trace、旧 `creator_human_review` 和 AI 委托审核声明，并证明 live 模式拒绝待人工复核的 fixed packet。
- Trigger Evaluator 使用可控的 Skill 发现响应测试 confusion matrix、precision、recall 和未确定状态。
- 一个显式产品集成测试使用 Claude Code 原生 Skill 发现，确认测试没有绕过真实触发路径。
- Paired comparison 测试验证两边都是 fresh run、case 对齐、协议 hash 相同，任何不兼容条件都会拒绝比较。
- L2 报告测试验证四种翻转分类、成本差异和 Trace 链接，不通过整页快照锁定样式。
- 课程测试验证 demo Creator 波动时参考 Skill 能继续流程，但报告清楚标记使用了兜底。

## Out of Scope

- 通用 Skill marketplace、远程发布和跨用户安装管理。
- 为多个 Agent 产品实现不同 Skill 发现机制。
- 把失败轨迹混入 v0 seed 或让 Creator 读取 selection/final。
- 用自定义关键词分类器替代 Claude Code 原生触发行为。
- 在 Creator 阶段做无界 Skill 重写、自动迭代或 selection 优化。

## Further Notes

- 第一课的目标是先让学习者看见差别，不提前证明稳定提升；定量结论来自第七课的 paired comparison。
- description 同时影响触发 precision 和 recall，不能只优化其中一个指标。
- Skill hash 必须来自规范化的完整可安装内容，而不是只 hash 主文档。
