# 跨模块契约 Spec

## Problem Statement

系统中的 Engine、Shop、Evaluation、Runner、Skill 和 Evolution 会交换同一批记录。多个 Agent 如果各自在模块中定义相似的 `Trace`、`StateDiff`、状态枚举或金额格式，代码可以局部通过，却会在集成时出现循环依赖、字段漂移和无法重放的历史产物。并行开发需要先固定记录归属和序列化不变量，同时避免提前设计完整未来 schema。

## Solution

采用 producer-owned contracts。产生记录的模块负责其语义和 canonical schema，消费者直接依赖该 schema，不复制类型。所有持久化顶层记录带版本和类型，使用规范 JSON 写入不可变 artifact；跨模块只传记录或 artifact reference，不传模块内部对象、绝对路径和 Provider 私有 payload。

## Contract Ownership

| Contract family | Producer | Main consumers |
| --- | --- | --- |
| `RuntimeConfig`, `ModelLock`, `WorkspaceRef` | Foundation Runtime | Engine、Runner、CLI |
| `EngineRequest`, `EngineEvent`, `EngineResult`, `Usage` | Foundation Runtime | Evaluation、Runner、Simulator、Creator |
| `CaseDefinition`, `DataLineage`, `SplitVisibility` | Testset Pipeline | Shop、Evaluator、Creator、Gate |
| `Money`, `ShopSnapshot`, `StateDiff`, `PolicyDecision`, `ToolResult` | Shop Environment | Evaluation、Testset、Reports |
| `Trace`, `EvidenceRef`, `AssertionResult`, `CaseGrade` | Evaluation & Judges | Runner、Reports、Evolution |
| `RunRecord`, `BudgetState`, `ComparisonRecord` | Simulation/Runner | Reports、Gate、Automation |
| `SkillArtifact`, `TriggerResult` | Skill Creation | Runner、Evolution、Portfolio |
| `FailureEvidenceFixture`, `FailureCardSet`, `Patch`, `CandidateArtifact`, `EvolutionPipelineSummary`, `GatePolicy`, `SelectionPairEvaluation`, `GateErrorEvidence`, `GateDecision`, `RegistryEvent` | Evolution & Governance | Skills、Runner、Automation、Reports、Portfolio |
| `SkillArtifactManifest`, `SkillManifestFile` | Skills | Evolution、Installer、Reports |
| `LoopState`, `PortfolioManifest` | Automation & Portfolio | CLI、Course Delivery |

Producer ownership 指语义归属，不允许 producer 把实现细节塞进接口。共享 schema 的源码位置和当前 wave owner 由并行实施文档管理。

## Serialization Rules

- 跨模块记录使用 Pydantic v2，默认 `frozen=True`、`extra="forbid"`。模块内部临时结构不强制使用 Pydantic。
- 每个持久化顶层记录包含 `schema_version` 和 `record_type`。首版使用 `v1alpha1`；不兼容变更创建新版本和显式迁移器。
- ID 是 opaque string。源数据 ID 保留上游 ID；run、iteration、candidate 和 event ID 在各自作用域唯一。
- 事件顺序由单调递增的 `sequence` 决定，不依赖 wall-clock timestamp。时间统一为 UTC RFC 3339。
- 业务金额使用 `Money(amount_minor: int, currency: str)`；退款和 Judge 不使用二进制浮点。模型费用使用十进制字符串和货币代码，保留低于最小货币单位的精度。
- Artifact reference 使用 workspace 或 run 根目录下的相对 POSIX path，并携带 SHA256。记录中不保存本机绝对路径。
- Canonical hash 基于稳定字段顺序和规范序列化。展示摘要、临时时间和本机路径不参与内容 hash。
- Provider 原始事件可以作为受控扩展 artifact 保留；核心消费者只读取规范化 `EngineEvent` 和 `Trace`。
- `pass`、`fail`、`not_evaluated`、`error`、`budget_stop` 等状态使用共享 enum，不用自由字符串制造同义状态。
- Contract 不包含凭据、请求头、hidden gold、selection/final 私有答案或角色无权读取的字段。
- `SelectionPairEvaluation` 是 Gate 私有记录。它只使用 `GatePolicy` 锁定的 opaque slot 和 critical-slot 子集，并把 pair 摘要及两侧 event logs 固定到 `iteration-0`。它不保存 selection case ID、题面、gold 或参考轨迹；candidate-facing 消费者只能读取 `GateDecision` 的 aggregate projection。
- `GateRequest` 显式携带 Registry 初始化时创建的稳定 experiment `lineage_id`；Gate 必须把它原样写入 `GateDecision`，不能根据每代 promoted parent hash 重新派生 lineage。
- `GatePolicy` 必须锁定 Trigger prompt set 的有序内容 hash 和 Trigger model ID。`TriggerEvalResult` 必须与该 prompt set、model ID、candidate Skill hash、measurement kind 和测量时间一致；证据漂移时 Gate 必须在 selection 前拒绝。
- `GateErrorEvidence` 是 Gate 与 Registry 共享的 canonical Pydantic record。它只能保存 gate stage、异常类型和可选 HTTP 状态码，不能保存 Provider 原始消息、路径、请求头或凭据。
- `GateDecision` 固定记录 candidate validation、Static、Trigger、fresh selection pair、关键回归、总体质量、成本和预算八个 stage，并通过 `ArtifactRef` 绑定完整 canonical `GatePolicy`。短路后的 stage 必须显式保存为 `not_evaluated`，不能复用旧 run 补齐。`GateDecision.metrics.total_cost_amount` 必须等于 Trigger、accepted selection 和 candidate selection 三部分成本之和。
- 每个 `GateStep` 的 stage、status、reason 和 evidence 数量必须匹配固定矩阵。例如 selection `judge_error` 必须是带完整 pair evidence 的 `error`，adapter error 才能使用单份脱敏 error receipt；调用方不能把二者换标。
- Registry 必须对 accepted 和 rejected `GateDecision` 都重算可用 evidence 的 aggregate metrics，并核对首个终止 stage、status 和 reason。拒绝决定不能因为“不提升”而跳过语义验证。
- `RegistryEvent` 使用连续 `sequence`、`previous_event_sha256` 和覆盖完整 event payload 的 `event_sha256` 建立 hash chain。Registry state 只能通过验证并重放完整 event log 得到，不能把可变 accepted pointer 文件当成事实来源。
- Registry hash chain 不能单独证明链尾未被干净删除。需要覆盖该威胁的部署必须在 Registry 外保存受信的 head hash 和 event count checkpoint，并在 audit 结果用于提升或回滚前比对。
- Registry 中的 Skill 版本按内容 hash 保存完整 manifest-declared runtime files。Promotion 和 rollback 只追加 event 并切换重放得到的 pointer，不覆盖 parent、历史版本、GateDecision 或 rejected candidate。
- `fixed` gate 必须标记 `synthetic_offline` 且 `network_used=false`；`live` gate 必须标记 `live_measured`，并且只有真实 Provider 请求发生后才能记录 `network_used=true`。`live` Trigger 必须提供与 policy 一致的货币成本；缺失成本时必须拒绝。两类结果不能互相回填。
- Gate 只能读取调用方显式传入并验证为 locked selection 的 manifest。它必须在读取前拒绝任何 symlink 路径组件，并同时检查词法路径和 resolved 路径中的 final split 名称；它也不能扫描 protected-data 目录寻找替代输入。
- 运行 `live` selection 需要受信的私有 6-case runner/catalog。仓库内的 lock anchor 不包含可执行题面、gold 或 runner；运行环境未注入该私有资产时，Gate 必须 fail closed，不能用 develop catalog、fixed fixture 或 Provider smoke test 冒充 live selection。

## Change Protocol

1. Producer 提交 contract proposal，说明调用方、迁移影响和新增测试。
2. Contract owner 更新 canonical schema 和 producer-consumer contract test。
3. 消费者 rebase 后适配；在适配完成前不合并接口变更。
4. 已写入 artifact 的字段不原地改义。需要改义时增加版本和迁移器。

当前 ticket 只定义它实际使用的字段。不要为了未来模块一次性填满所有 contract family。

## Testing Decisions

- 每个 contract 测试 JSON round-trip、未知字段拒绝、enum 值、时间和金额规范化。
- 每条跨模块路径至少有一个 producer-to-consumer contract test。
- Hash 测试证明字段顺序不改变结果，业务字段变化一定改变结果。
- Artifact reference 测试拒绝绝对路径、目录穿越和 checksum 不匹配。
- Gate contract 测试拒绝 Trigger prompt/model 漂移、live 成本缺失、selection symlink 和 final 路径，并证明 fixed 证据不能通过 live 标记约束。
- 脱敏测试递归扫描序列化结果，不允许凭据和隐藏字段进入记录。
- 版本测试读取当前版本，并对未来版本返回明确的 unsupported-version 错误。

## Out of Scope

- 通用事件总线、schema registry 服务、代码生成平台和跨语言协议。
- 为尚未进入当前 ticket 的未来功能预定义所有字段。
- 把 Provider 私有响应直接变成系统公共接口。

## Further Notes

- Issue #2 集成前必须先冻结最小 `EngineEvent`、`ShopSnapshot`、`StateDiff`、`Trace`、`AssertionResult` 和 `CaseGrade`。
- 深模块仍然拥有自己的接口；contracts 只表达跨 seam 的稳定事实，不承载业务实现。
