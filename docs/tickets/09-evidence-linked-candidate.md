# Ticket 09：有证据链接的候选 Skill

## 交付范围

本 ticket 实现 Evolution & Governance 的 evidence-to-candidate vertical slice。
它只负责失败诊断、结构化补丁、candidate 物化和 Updater 隔离，不实现 Ticket 10
的 selection gate、registry、promote 或 rollback。

## Canonical contract

`ses.contracts.evolution` 是本 ticket 的 producer-owned contract，包含：

- `FailureCard`：`trigger`、`pattern`、`overload`、`terminology`、`timing`、`safety` 六类。
- `FailureAttribution`：固定顺序 `runtime/environment`、`case/gold`、`Judge/Simulator`、`Skill`。
- `Patch`：有序的 `add`、`update`、`delete` 操作。每个操作记录 target、前置内容 hash、Trace evidence、Assertion evidence、理由、风险和 Failure Card ID。
- `CandidateArtifact`：parent Skill hash、Patch hash、完整可安装 runtime files、manifest、content hash 和 Static Gate 结果。
- `FailureEvidenceFixture`：只保存 paired comparison 的最小脱敏摘要。
- `FailureCardSet`：把同一 fixture 生成的卡片、provenance 和 fixture hash 绑定为
  一个严格、可复核的记录。

`FailureEvidenceCase.failure_kinds` 保存机械提取的失败 Assertion 类型；
`failure_categories` 保存经轻量模型辅助、人工确认后的六分类。分析器要求每个失败 case
恰好有一个审核分类，不会把 `tool-order`、`state-diff` 等 Assertion 名称直接猜成语义
类别。`judge_simulator_health` 独立保存 Judge/Simulator 协议审核结论。导出器默认写
`not_reviewed`；审核者必须明确确认 `healthy`，分析器才允许继续归因。课程 synthetic
fixture 固定提供两项审核结果；真实 fixture 未审核时明确停止。

所有顶层记录使用 `v1alpha1` 和严格 extra-forbid schema。操作目标只能是
`SKILL.md` 或 `references/*.md`。应用器在内存中完成全部 precondition、冲突、证据
和目标检查，任何错误都会使整个操作失败，不返回部分结果。

## 归因和安全边界

Updater 先排除 runtime/environment，再排除 case/gold，再排除 Judge/Simulator，
最后才能把失败归因给 Skill。前三级任何一项失败都不会生成 Skill patch。存在 Assertion
只代表 Judge 产出了记录，不代表 Judge/Simulator 健康；`unhealthy` 和 `not_reviewed`
都会停止流程。Ticket 08 live fixture 的三个 `infrastructure_error` 没有 Assertion
evidence，分析器也会直接拒绝生成 Skill patch。

可信 Failure Analyzer 先读取 fixture 并生成 `FailureCardSet`。Updater workspace 只复制：

1. 已生成的 Failure Card set；
2. 允许的 Updater Skill 规范；
3. accepted parent 的 manifest 和 manifest 声明的 runtime 文件。

它不复制原始 evidence、源码、凭据、gold、selection/final 数据、Judge 私有材料或
provider stream。
证据 fixture 只提交相对标签和 SHA256，不提交原始日志或绝对路径。

## Candidate 生命周期

1. 校验 parent Skill schema、content hash、evidence Skill hash 和 Patch parent hash。
2. 校验每张 Failure Card 的 case、Trace/Assertion 类型和 fixture hash。
3. 要求每个 operation 的 evidence 与它声明的全部 Failure Card 完全一致。
4. 执行每 operation 12 行、每 Patch 24 行和最多 3 个 operation 的教学预算。
5. 在内存副本上按固定顺序应用 add/update/delete。
6. 在临时目录写入完整 candidate，重建 manifest并运行 Ticket 08 Static Gate。
7. 通过后原子发布包含 evidence、cards、Patch、candidate record 和完整 Skill 的
   bundle；parent 永远保持不变。

默认 `ses evolve --mode fixed` 使用 FakeUpdater，离线复现完整流程。显式
`--mode live` 使用锁定的 Claude Code + SiliconFlow Creator 模型提出结构化操作；
它仍要求输入已完成语义分类审核。hash、evidence、范围预算和应用由本地确定性代码控制。

默认流程和自动测试不执行付费 Provider；只有显式 `--mode live` 才发起网络调用。
本 ticket 不读取 selection/final，也不改变 accepted 指针。
