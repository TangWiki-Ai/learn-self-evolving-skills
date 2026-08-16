# Contracts

`ses.contracts` 是模块之间唯一的共享记录入口。生产者创建 canonical model；消费者直接导入它，不复制相似类型。当前 wire version 是 `v1alpha1`。

## Ownership

| Contract | Producer | Main consumers |
| --- | --- | --- |
| `EngineRequest`, `EngineEvent`, `Usage` | Foundation Runtime | Evaluation、Evaluator、Runner、Simulator、Creator |
| `CaseDefinition` | Testset Pipeline | Shop、Evaluation、Evaluator、Creator、Gate |
| `Money`, `ShopSnapshot`, `StateDiff`, `ToolResult` | Shop Environment | Evaluation、Testset、Reporting |
| `Trace`, `EvidenceRef`, `AssertionResult`, `CaseGrade` | Evaluation & Judges | Runner、Reporting、Evolution |
| version、IDs、`ArtifactRef`、artifact wire/content hash | Contract owner | 所有模块 |

顶层持久记录必须显式提供 `schema_version` 和固定 `record_type`；读取时不补默认值。`Money`、`Usage`、`EvidenceRef` 等嵌套值对象不重复这些字段。

## Invariants

- 所有 model 使用 Pydantic v2、`frozen=True` 和 `extra="forbid"`；验证会把嵌套 JSON object/array 复制成只读 mapping/tuple，producer 不得原地改写已验证记录。`model_construct()` 是 Pydantic 的可信绕过入口，不属于 contract 输入路径。
- 时间统一转成 UTC，并按 RFC 3339 `Z` 形式输出。
- Artifact 只引用 workspace 或 run root 下的 canonical 相对 POSIX path，并携带实际落盘 wire bytes 的小写 SHA-256。Loader 还必须 resolve root 与目标、拒绝 symlink 越界，并对同一份已校验 bytes 解析，避免 TOCTOU。
- `artifact_json_bytes(record)` 生成完整 wire bytes：稳定 key 顺序、紧凑分隔符和 UTF-8，并保留版本、类型、时间与展示摘要等全部字段。写入方对这份原始 bytes 计算 `ArtifactRef.sha256`；读取方先对同一份 bytes 验证 checksum，再调用对应类型的 `model_validate_json()`。
- `content_sha256(record)` 只计算语义内容 hash。当前投影递归排除 `EngineEvent.occurred_at`、`ShopSnapshot.captured_at` 和 `StateDiff.summary`；它不能替代 artifact wire digest。
- 任意嵌套 JSON payload 都拒绝凭据、请求头、hidden gold 和私有答案字段。这个结构扫描不猜测自由文本中的秘密；producer 仍须按已知凭据值脱敏 message、summary 和 reason。
- `EngineEvent` 用 `message_id` 组合 assistant text/tool-call 片段。一个 `Trace` 对应一个 `EngineRequest`；`UsagePayload` 是该请求在当前 sequence 的累计值，最后一条必须等于 Trace 汇总。Trace 恰好以一个一致的 `completed` 事件结束。
- Shop-owned `ToolResult` 不保存 Engine transport ID；`EngineEvent.ToolResultPayload.tool_call_id` 独占调用关联。Shop JSON 拒绝二进制浮点，业务金额使用 `Money` wire shape。
- Judge 在构造 `EvidenceRef` 前，由调用方先持久化 source record 并注入 `ArtifactRef`。这项顺序属于 Evaluator/Runner orchestration，不属于 Judge 或 artifact contract。
- Contracts 只保存跨 seam 的事实。Provider 原始 payload、Shop 内部对象和 Judge 业务逻辑留在生产者模块。
- `CaseSplit` 只包含 Ticket 07 已经持久化并执行写保护的 `develop`、`selection` 和 `final`。`selection`、`final` 可用于验证锁定 manifest，但 qualification 入口只允许写 `develop`。

## Change process

1. Producer 先提交 proposal，列出字段、类型、不变量、生产者、消费者、调用示例和迁移影响。
2. Contract owner 更新 canonical model 和 producer-consumer contract tests。
3. 消费者 rebase 后适配；适配完成前不合并 contract 变更。
4. 只有所有 producer/consumer 同步更新后，才能在当前 alpha 版本增加字段。已发布 artifact 的不兼容变更创建新 schema version 和显式迁移器；已持久化字段不原地改义。

当前 Ticket 之外的字段不要提前加入。领域 lane 发现缺口时，在 handoff 中记录 proposal，而不是创建第二套 model。
