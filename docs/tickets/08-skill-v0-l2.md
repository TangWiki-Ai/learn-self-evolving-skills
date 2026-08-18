# Ticket 08：Skill v0 与 paired L2

## 交付路径

`ses skill-v0-pipeline` 连接五个生产 seam：Creator seed 隔离、Static
Gate、Claude Code 原生 Trigger Eval、fresh paired Runner 和 L2 renderer。
默认模式使用固定输入和 FakeCreator，不读取 Key、不访问网络。

## Creator seed

`data/skill-v0/creator/seed-manifest.json` 固定 9 条 creator split 成功
Trace。构建脚本要求 STATE-Bench 位于 commit
`5644b1838d96bc4483da29642d058ecaa6f80f7f`，并验证 origin 和 27 个源文件。
它逐条重放原始工具调用、核对每个返回、从前后快照计算 StateDiff，再执行
STATE-Bench State Judge。它不再用 task 的预期字段伪造“实际”状态。

每条记录保存来源、replay receipt、Trace、StateDiff、State Grade、模型 Judge
输入、模型原始 run、Model Grade、projection 和课程 attestation 的 SHA256。锁定的
live 模型 Judge 使用 `rubric-prompt-v3`；课程 attestation 只绑定上述证据，状态为
`course_authored_pending_human_review`，不声称人已批准。Loader 会重算并核对整条
证据链。fixed/offline 可以用它复现课程；live Creator 在独立签名人审接入前关闭，
待处理项目集中在 `docs/release/human-review-packet.md`。

Creator workspace 只复制 `projections/*.json` 和 `skill-spec.md`。完整源 Trace
保留在 `private/traces/`，不会进入 Creator workspace。Ticket 07 的 15 条
develop case 不参与 v0 seed。

## Artifact 与 gate

v0 manifest 声明 runtime include allowlist、来源版本、完整规范化内容 hash 和
`claude-code-native` 兼容字段。安装器只复制 manifest 中的 `SKILL.md` 和
`references/`。

Static Gate 在 Trigger 或 paired evaluation 前运行，并逐项报告 metadata、
manifest、文件清单、工具、标识、固定答案、eval 内容、危险指令和长度。失败
报告可审计，但调用链立即停止。`allowed-tools` 必须写 Claude Code 实际识别的
`mcp__shop__get_order`、`mcp__shop__get_policies` 和
`mcp__shop__process_return`，不能使用只适用于课程内部的短名称。

## Trigger 与 paired

Trigger 集固定 10 条正向和 10 条负向 prompt。Offline backend 回放原生发现
形状；显式 live backend 为每条 prompt 建立新 Claude workspace，只安装候选
Skill，并观察 Claude Code `Skill` tool call。报告保留 TP/FP/TN/FN、P/R、
未确定状态、逐 prompt 证据、Skill hash 和引擎版本。

Fixed paired comparison 在 Ticket 07 的 15 条 pending course develop cases 上创建两套
新 Runner。每次 attempt 都创建新 workspace 和 Shop 环境；完成型结果必须提供
Trace、StateDiff 和 CaseGrade，timeout 等异常则保留状态和已经产生的证据。
比较器要求 case plan、iteration、data、模型锁和协议完全一致；
Skill hash 是实验变量。这 15 条只允许 fixed/offline；live paired 会在调用 Provider
前要求独立签名的人审记录。Canonical `PairedComparison` 位于
`ses.contracts.runner`。Live 两侧使用相同 Claude Code 原生 discovery 参数和
MCP allowlist；只有 Skill 侧 workspace 安装候选。Rule Judge 忽略 `Skill`
元调用，但仍严格检查业务工具顺序。

比较器把两侧 event log、每条 Trace、StateDiff 和 CaseGrade 都绑定到同一个
artifact root。Contract 重算 case 分类、分数、token、费用、耗时和 execution
hash。L2 renderer 会从 event log 重新推导整份 comparison，并在任何引用或
Skill hash 不一致时拒绝输出。

## Fixed 与 live 声明

`course/ch07-create-v0/artifacts/` 是 fixed/offline reference。它真实执行
Runner、Shop 和 Judge，但 Agent 输出来自 deterministic fake engine。Fixed
模式规范化 Trace 事件时钟；相同代码和输入会生成相同的 event、comparison 和
L2 artifact hash。

`--mode live` 使用 `models.lock.json` 的 Creator 和 Main 角色，并只从进程
环境读取 `SILICONFLOW_API_KEY`。Creator、Trigger 和 paired 都走真实 Claude
Code headless；只有实际完成两侧 15-case run 时，paired 才标记
`live_measured`。Live artifact 必须写入临时目录，不能覆盖课程参考结果。
