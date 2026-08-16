# Ticket 08：Skill v0 与 paired L2

## 交付路径

`ses skill-v0-pipeline` 连接五个生产 seam：Creator seed 隔离、Static
Gate、Claude Code 原生 Trigger Eval、fresh paired Runner 和 L2 renderer。
默认模式使用固定输入和 FakeCreator，不读取 Key、不访问网络。

## Creator seed

`data/skill-v0/creator/seed-manifest.json` 固定 9 条 creator split 成功
Trace。每条记录保存源文件与 projection 的 SHA256，以及 State Judge、模型
Judge 和人工审核结论。Loader 同时验证数量、split、三重结论、唯一性和两个
hash。

Creator workspace 只复制 `projections/*.json` 和 `skill-spec.md`。完整源 Trace
保留在 `private/traces/`，不会进入 Creator workspace。Ticket 07 的 15 条
develop case 不参与 v0 seed。

## Artifact 与 gate

v0 manifest 声明 runtime include allowlist、来源版本、完整规范化内容 hash 和
`claude-code-native` 兼容字段。安装器只复制 manifest 中的 `SKILL.md` 和
`references/`。

Static Gate 在 Trigger 或 paired evaluation 前运行，并逐项报告 metadata、
manifest、文件清单、工具、标识、固定答案、eval 内容、危险指令和长度。失败
报告可审计，但调用链立即停止。

## Trigger 与 paired

Trigger 集固定 10 条正向和 10 条负向 prompt。Offline backend 回放原生发现
形状；显式 live backend 为每条 prompt 建立新 Claude workspace，只安装候选
Skill，并观察 Claude Code `Skill` tool call。报告保留 TP/FP/TN/FN、P/R、
未确定状态、逐 prompt 证据、Skill hash 和引擎版本。

Paired comparison 在 Ticket 07 的 15 条 qualified develop cases 上创建两套
新 Runner。每次 attempt 都创建新 workspace、Shop 环境、Trace、StateDiff 和
CaseGrade。比较器要求 case plan、iteration、data、模型锁和协议完全一致；
Skill hash 是实验变量。Canonical `PairedComparison` 位于
`ses.contracts.runner`。

## Fixed 与 live 声明

`course/ch07-create-v0/artifacts/` 是 fixed/offline reference。它真实执行
Runner、Shop 和 Judge，但 Agent 输出来自 deterministic fake engine。

`--mode live` 使用 `models.lock.json` 的 Creator 和 Main 角色，并只从进程
环境读取 `SILICONFLOW_API_KEY`。当前 live vertical slice 实测 Creator 和
Trigger；paired 部分仍标记 `paired_live_model_measured=false`，不冒充线上
Agent paired 质量。Live artifact 必须写入临时目录，不能覆盖课程参考结果。
