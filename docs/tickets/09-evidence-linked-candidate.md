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
- `FailureEvidenceFixture`：只保存 live paired comparison 的最小脱敏摘要。

所有顶层记录使用 `v1alpha1` 和严格 extra-forbid schema。操作目标只能是
`SKILL.md` 或 `references/*.md`。应用器在内存中完成全部 precondition、冲突、证据
和目标检查，任何错误都会使整个操作失败，不返回部分结果。

## 归因和安全边界

Updater 先排除 runtime/environment，再排除 case/gold，再排除 Judge/Simulator，
最后才能把失败归因给 Skill。前三级任何一项失败都不会生成 Skill patch。Ticket 08
live fixture 的三个 `infrastructure_error` 没有 Assertion evidence，分析器直接拒绝
生成 Skill patch。

Updater workspace 只复制：

1. 脱敏 failure evidence fixture；
2. accepted parent 的 manifest 和 manifest 声明的 runtime 文件。

它不复制源码、凭据、gold、selection/final 数据、Judge 私有材料或 provider stream。
证据 fixture 只提交相对标签和 SHA256，不提交原始日志或绝对路径。

## Candidate 生命周期

1. 校验 parent Skill schema、content hash 和 Patch parent hash。
2. 校验全部 Failure Card、Trace/Assertion evidence、操作冲突和 precondition hash。
3. 在内存副本上按固定顺序应用 add/update/delete。
4. 在临时目录写入完整 candidate，重建 manifest。
5. 运行 Ticket 08 Static Gate。
6. 通过后用原子 rename 发布新的 candidate 目录；parent 永远保持不变。

本 ticket 不执行付费 Provider，不读取 selection/final，也不改变 accepted 指针。
