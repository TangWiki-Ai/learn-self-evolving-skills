# Ticket 10：门控并治理 Skill 版本

## 交付范围

本 ticket 实现 Evolution & Governance 的 candidate selection gate 和版本
Registry 纵向切片。它从 Ticket 09 的 `CandidateArtifact` 开始，重新验证 accepted
parent 与 candidate，按固定顺序运行 gate，持久化完整 `GateDecision`，再用
append-only `RegistryEvent` 记录 candidate、接受、拒绝、提升和回滚。

本 ticket 不实现 Issue #11 的自动进化循环，也不读取或运行 final split。默认测试、课程
fixture 和参考产物只使用 `fixed` 模式，并明确记录为 `synthetic_offline`；它们不能冒充
live 结果。

## Canonical contract

`ses.contracts.evolution` 继续拥有 Evolution & Governance 的 canonical records。本 ticket
在 Ticket 09 契约上增加：

- `GatePolicy`：锁定 gate 阈值、Trigger prompt set hash、Trigger model ID、六个 opaque
  selection slot、critical slot 子集、selection inventory hash、评测协议 hash、
  模型锁 hash、质量、成本和 token 预算。
- `SelectionPairEvaluation`：保存同一批六个锁定 selection slot 上 accepted 与 candidate
  在 `iteration-0` 的 fresh paired evidence。逐 case 记录只使用 `slot-001` 这类 opaque ID，
  不保存题面、gold、参考轨迹或真实 case ID。
- `GateDecision`：保存双方 Skill hash、稳定 experiment lineage、协议身份、完整锁定
  `GatePolicy` 的 `ArtifactRef`、运行模式、每一级 gate 状态、聚合指标、最终决定、原因和
  evidence references。
- `GateErrorEvidence`：保存 credential-safe 的失败 stage、异常类型和可选 HTTP 状态码；
  它禁止 Provider 原始消息和额外字段。
- `RegistryEvent`：保存一个有 hash chain 的不可变 Registry transition。

这些是对 `v1alpha1` 的增量扩展。Ticket 09 的 `FailureCardSet`、`Patch` 和
`CandidateArtifact` 不改义。Gate 直接读取 canonical `CandidateArtifact`，并重新核对
`parent_skill_sha256`、candidate files、manifest 和 `content_sha256`；它不复制或弱化
Ticket 09 的 lineage。

## Gate 顺序和保守决策

Gate 必须按以下顺序执行：

```text
candidate validation
  → Static Gate
  → Trigger Gate
  → fresh accepted-vs-candidate selection pair
  → critical case regression
  → overall quality
  → cost
  → budget
```

`GateDecision.steps` 必须完整包含八个 `GateStage`，顺序不能改变。任一级失败后，Gate
停止后续付费工作，并把余下 stage 记为 `not_evaluated`。它不能省略未执行 stage，也不能
把失败后的旧结果拼进本次决定。

`GateRequest` 必须显式携带 Registry 初始化时创建的稳定 `lineage_id`。Gate 将该值原样写入
`GateDecision`，不能根据本代 promoted parent 的 hash 重新生成 lineage。它还要把本次
`GatePolicy` 作为 canonical artifact 落盘，并同时保存该 artifact 的 `ArtifactRef` 和 policy
content hash。Registry 在记录决定时重新验证两者。

默认策略只接受严格总体改进。以下情况一律得到 `rejected`：

- candidate 或 Static Gate 无法验证；
- Trigger prompt set、prompt hash 或 model ID 与 policy 不匹配；
- Trigger precision、recall 或 indeterminate 数不满足锁定阈值；
- `live` Trigger 没有返回可验证的货币成本，或货币与 policy 不一致；
- paired run 不 fresh、协议或 Skill hash 不匹配、缺 case 或缺 evidence；
- 任一 accepted 或 candidate 结果出现 Judge error、其他 evaluation error 或
  `budget_stop`；
- 关键 case 从 pass 退到非 pass；
- 总体质量平局或回退；
- candidate 绝对成本、相对 accepted 成本增幅、Gate 总成本或 token 用量超限。

`GateDecision.metrics` 只暴露 aggregate：Trigger P/R、selection 数量、双方 pass
count/rate、质量差、关键回退数、Trigger 成本、双方 selection 成本、三者总成本、
相对成本增幅以及总 token。公开决定不保存
selection 题面、gold、逐题业务证据或真实 case ID。

## Selection 隔离和 fresh evidence

调用方必须显式传入一个 `split == "selection"` 且 `locked == true` 的 manifest。Gate
只读取这个文件，不扫描 protected-data 目录。Gate 在读取 bytes 前拒绝目标文件或任何
父路径中的 symlink，并同时检查词法路径和 resolved 路径是否出现 final split 名称。
Gate policy 保存 selection manifest 的 wire SHA-256，并要求本次运行仍与该 hash 一致。

每次 selection pair 必须绑定：

- 唯一 `gate_id` 和本次生成的 `evaluation_nonce`；
- 固定 `iteration-0`，并要求 pair 摘要和两侧 event logs 完全一致；
- 不同的 accepted/candidate Skill hash、run ID 和 event artifact；
- 同一个 selection lock、evaluation protocol、model lock、测量时间和货币；
- 恰好使用 policy 固定顺序中的六个 opaque slot，critical 标记与 policy 子集完全一致；
- 两份 fresh event logs 及其 `ArtifactRef` checksum。

完整 paired artifact 属于 Gate 私有 evidence。Updater 或 candidate-facing 输出只能接收
`GateDecision` 的 aggregate projection 和允许公开的原因；它不能取得私有 artifact 的读取
权限。

## Fixed 和 live

`fixed` 模式使用确定性 adapter，`measurement_kind` 必须为 `synthetic_offline`，并且
`network_used` 必须为 `false`。它只用于默认测试、课程演示和固定审计产物。
即使本机同时运行了 Provider smoke test，fixed Gate 也不能因此改标为 live。

`live` 模式必须使用实际 Provider 产生的 fresh Trigger 与 selection runs，
`measurement_kind` 必须为 `live_measured`。只有实际发出网络请求时才能记录
`network_used == true`；否则 Gate 不得接受 candidate。Provider 错误、HTTP 402、Judge
error 和不完整结果都要如实拒绝，不能回填 fixed fixture 或伪造 live 指标。
`live` Trigger 还必须返回与 policy 一致的货币成本。Gate 把该费用与 accepted/candidate
selection 费用相加后再检查总成本预算。

仓库中的 selection lock 只是 lock anchor，不是可执行的 6-case catalog。完整 `live`
selection 必须由运行环境注入受信的私有 6-case runner/catalog，并把运行证据投影为
opaque slots。没有该私有资产时，本 ticket 只能执行 fixed Gate；Provider transport 或
Trigger smoke test 不等于 live selection Gate。

## Append-only Registry

`SkillRegistry` 通过一个小 interface 管理全部 transition：`initialize`、
`register_candidate`、`record_decision`、`promote`、`rollback` 和 `audit`。调用方不能直接
改 accepted pointer；`audit` 只从验证过的 events 重放完整状态。

Registry 依次记录：

```text
registry_initialized
  → candidate_registered
  → candidate_accepted | candidate_rejected
  → promoted
  → rolled_back
```

接受 GateDecision 只把 candidate 标为已验证；`promoted` 才切换 current accepted pointer。
被拒 candidate 和它的 evidence 永远保留，不能删除。Promotion 必须确认 GateDecision
接受了该 candidate，而且 decision 的 accepted parent 仍是 current accepted；过期决定不能
提升。

每个 event 保存连续 `sequence`、`previous_event_sha256` 和覆盖完整 payload 的
`event_sha256`。第一条 event 的 previous hash 为 64 个零。Registry 每次读取时验证 schema、
self hash、前向 hash link、连续 sequence、唯一 event/command ID、artifact checksum 和状态
迁移。任一检查失败时，Registry fail closed。

命令通过 `command_id` 和命令意图的 `command_sha256` 实现幂等。同一 ID 与同一意图返回
原 event，不追加新行；同一 ID 不能改义。

## Lineage、不可变版本和回滚

Registry 把每个完整 Skill 复制到 `versions/<content_sha256>/`，只保存 manifest 声明的
runtime files。candidate record、GateDecision 和 verification evidence 使用
content-addressed `ArtifactRef` 关联。已有 version 或 object 只能在 bytes 与 hash 完全一致时
复用，不能覆盖。

`CandidateArtifact.parent_skill_sha256` 建立版本 lineage。Promotion 和 rollback 只改变
current accepted pointer，不改 parent edge，也不重写旧 event。回滚目标必须：

- 已存在于同一 Registry lineage；
- 已通过 gate 或作为初始 accepted 版本提供了可验证 evidence；
- 曾经成为 current accepted；
- 当前仍能通过 manifest、runtime content 和 evidence checksum 验证；
- 不是 candidate、rejected 或当前指针本身。

回滚会把离开的版本标为 `rolled_back`，把目标恢复为 `accepted`，并追加新 event。它不删除
任何后代或 rejected branch。从回滚目标创建的新 candidate 会形成一条可追溯分支。

Judge、Simulator、selection lock、evaluation protocol 或 gate policy 的身份变化必须开始新
实验 lineage。系统不能把不同协议的分数接到同一条进化曲线。

## 实现边界和已知风险

- 公开仓库只提交 opaque selection/final lock：数量、通用 slot、协议、固定上游版本和整体
  commitment。逐题请求、source identity、fixture、确定性 oracle、rubric、选题 key、semantic
  mapping 和完整 inventory 全部留在仓库外。选题使用外部至少 32-byte key 做 HMAC 排序；
  private inventory 通过 pointer/hash 绑定 mapping。实现不能内嵌 family 身份、依赖公开固定
  salt 或读取当前 Skill 的结果。`FixedGateAdapter` 仍只生成六个明确标记为
  `synthetic_offline` 的 opaque slot。完整 live Gate 还需要受信的私有 6-case runner；缺少
  它时，任何 Provider 请求都不能被报告为 live selection 结果。
- Gate 锁定 Trigger prompt set hash 和 model ID，并把 Trigger token 与货币成本纳入总预算。
  `fixed` Trigger 未提供货币成本时以明确的 synthetic zero 计量；`live` Trigger 缺失成本
  或货币不一致时直接拒绝。
- Gate 在取得可信 `CandidateArtifact` 身份和 accepted parent 后，会把 candidate 内容篡改、
  Static/Trigger/selection adapter error 保存为带脱敏错误 evidence 的 rejected decision。
  错误 evidence 只保存 stage、异常类型和可用的 HTTP 状态码，不保存 Provider 原始消息。
  若 candidate record 本身无法解析，Gate 会在 candidate validation 前置条件处 fail closed；
  它不会为无法识别的 bytes 伪造 candidate ID 或 decision。
- Hash chain 能检测内容改写、插入、重排和中间删除。Registry 在根目录外保存 head hash 与
  event count checkpoint；live 治理要求进程外 HMAC key，fixed/offline 默认 checkpoint 明确
  标记为 `local_untrusted`。这能检测事件日志被单独截尾或 checkpoint 被伪造，但单机可写
  文件无法阻止攻击者把“旧日志 + 当时真实旧 checkpoint”一起回放。需要覆盖该威胁的部署
  必须把 checkpoint 放进外部、防回滚、版本化的存储或单调计数器；本 ticket 不把本地 HMAC
  冒充单调新鲜度证明。
- 锁定的 return 候选池在排除 creator/已占用语义组后只有 19 个 eligible group，而
  selection+final 使用 18 个。protected mapping、eligible membership、精确排名、split、逐题
  身份和 gold 都不公开；但上游 33-task return source universe 本身公开且很小，18/19 的使用
  比例也过高。当前实现不声称强抗污染 secrecy；扩大 source pool 或加入经过验证的 keyed
  policy variants 是 Issue #10 继续保持打开的原因之一。

Registry append 使用进程锁，并在持锁后重新检查 sequence 与 head hash。Gate 与 Registry
都会解析 paired event logs，核对 run ID、nonce、Skill hash、slot、status、score、usage、
cost 和 currency。Registry 还会重新验证 GatePolicy、candidate、accepted manifest、Trigger、
paired metrics、首个失败 stage/reason 和 protocol identity；accepted 与 rejected decision
使用同一条语义验证边界。

应用服务把 Gate 写盘和 Registry append 作为可恢复的两阶段操作。如果 append 暂时失败，
同一 `gate_id`、candidate record bytes、policy、lineage、mode 和测量时间的命令会重新验证并
复用已持久化 decision，再幂等重试 Registry append；candidate bundle 身份变化时拒绝恢复，
它也不会重复运行付费 Gate。

CLI 提供 `ses registry init|register|promote|rollback|inspect|audit` 和
`ses gate candidate`。Gate CLI 只公开 fixed scenario，避免在没有私有 selection runner 时
误标 live；生产 live 路径通过 `GateEvaluationAdapter` 注入。

## 测试要求

- Gate decision table 覆盖接受、Trigger 拒绝、证据不足、Judge error、budget stop、关键
  回退、平局、总体回退、绝对和相对成本超限、token 预算超限。
- Trigger 契约测试拒绝 prompt set/hash/model 漂移和 live 费用缺失，并验证 Trigger 成本计入总预算。
- orchestration 测试断言 stage 顺序、短路和未发生多余付费调用。
- selection contract 测试拒绝 reused runs、wrong nonce、不同协议、错误 Skill hash、缺 slot、
  iteration mismatch、重复 slot、event mismatch 和私有数据泄漏。
- selection 路径测试使用 final 诱饵、symlink 文件和 symlink 父目录，验证 Gate 在读取前拒绝。
- Registry 状态机覆盖 initialize、candidate、accept、reject、promote、多代 lineage、rollback
  和分支。
- 幂等测试证明相同命令不追加 event，同 ID 改义会失败。
- 篡改测试覆盖 event、GateDecision、evidence、manifest 和 runtime content，并证明失败后
  accepted pointer 和历史 bytes 不变。
- CLI 集成测试从 Ticket 09 candidate 完成 gate、接受或拒绝、promote、lineage inspect 和
  rollback。默认环境必须移除凭据并阻断网络。
