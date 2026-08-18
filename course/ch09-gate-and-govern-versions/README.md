# 第 9 课：门控并治理 Skill 版本

本课把第 8 课生成的 candidate 放进保守门控，再用 append-only Registry 保存接受、
拒绝、提升和回滚历史。

流程固定为：

```text
immutable accepted parent + immutable candidate
  → candidate validation
  → Static Gate
  → Trigger Gate
  → locked selection fresh pair
  → critical-case regression
  → overall quality
  → cost
  → budget
  → accepted / rejected
  → explicit promote or verified rollback
```

## 为什么平局也拒绝

Gate 不负责证明 candidate 在所有未来任务上都更好。它只在固定协议和有限 selection
样本上阻止已经观察到的退化。因此本课采用保守规则：平局、Judge error、证据不足、
关键 case 回退、总体退化、费用增长和预算停止都拒绝 candidate。

某一级失败后，Gate 把后续步骤记为 `not_evaluated`。这样你能区分“检查失败”和“因为
前置失败而没有运行”，也不会在便宜检查已经失败后继续花费评测预算。

## selection 隔离

Gate 只接收锁文件及其 SHA256。它不会把 selection 题面、gold、逐题 case ID 或参考
轨迹放进公开 `GateDecision`。私有 paired evidence 使用 `slot-001` 到 `slot-006`
表示六个不透明位置，并把摘要和两侧日志固定到 `iteration-0`；公开记录只保存双方通过数、
质量差、关键回归数、token 和费用汇总。

入口只接受 selection lock。代码会在读取前拒绝任何 symlink 路径组件，并检查词法路径和
resolved 路径中的 `final`。本课不读取、不运行 final split。

Gate policy 还锁定 20 条 Trigger prompt 的有序 hash 和 model ID。Trigger 费用与两侧
selection 费用一起进入总预算；live Trigger 缺费用或币种不一致时直接拒绝。

## Fixed 模式不是 live

课程测试和 `artifacts/` 使用 `FixedGateAdapter`。它不访问网络，不读取 Key，并明确写入：

```text
mode=fixed
measurement_kind=synthetic_offline
network_used=false
```

这些产物用于学习 Gate 顺序、拒绝规则和 Registry 状态机。它们不是模型线上效果证据，
也不能改名为 `live_measured`。只有真实 Provider 和受信的私有 6-case runner 完成 Trigger
及 selection 两侧 fresh run 后，生产流程才能生成 live 决策。仓库只提交 lock anchor，
不包含私有题面、gold 或 runner，因此本课不会把 Provider smoke test 冒充 live Gate。

## Registry 为什么把 accept 和 promote 分开

`candidate_accepted` 表示 candidate 通过完整 Gate；它还没有改变当前 accepted 指针。
`promoted` 才把指针移到该版本。这个分离让人工流程和后续自动循环复用同一条治理路径，
也避免一个 Gate 返回值静默覆盖当前版本。

Registry 每次操作只追加一个 `RegistryEvent`。事件记录连续 sequence、上一事件 hash、
当前 accepted 指针、版本 manifest、GateDecision 和证据引用。Registry 重放全部事件得到
当前状态，并在重放时重新验证事件链、版本内容和 evidence hash。

Rollback 也只追加事件。它只能指向已经存在、曾经成为 current 且带验证证据的版本；
它不会删除 candidate、改写旧事件或覆盖历史 Skill 文件。

## Starter 与 solution

`starter/governance.py` 保留四个练习缺口：

1. 按固定顺序运行 candidate Gate；
2. 把 GateDecision 写入 Registry；
3. 只提升 accepted candidate；
4. 回滚到已经验证的历史版本。

`solution/governance.py` 不复制生产逻辑。它把课程参数交给
`ses.evolution.gate.run_candidate_gate` 和 `ses.evolution.registry.SkillRegistry`。

同一条生产路径也提供 CLI。下面的命令从干净工作区生成 Ticket 09 fixed candidate，初始化
Registry，再依次完成注册、Gate、提升、回滚和审计。Gate 命令会自动追加
`candidate_accepted` 或 `candidate_rejected`：

```bash
uv run ses evolve \
  --parent course/ch07-create-v0/artifacts/skill/v0 \
  --evidence tests/fixtures/evolution/synthetic-failure-evidence.json \
  --output .ses/candidate --mode fixed --json

uv run ses registry init \
  --registry .ses/registry \
  --accepted-skill course/ch07-create-v0/artifacts/skill/v0 \
  --evidence course/ch07-create-v0/artifacts/summary.json \
  --command-id command-init --occurred-at 2026-08-18T10:00:00Z \
  --json > .ses/registry-init.json

uv run ses registry register \
  --registry .ses/registry --candidate-bundle .ses/candidate \
  --command-id command-register --occurred-at 2026-08-18T10:00:01Z \
  --json > .ses/registry-register.json

SES_CANDIDATE_ID="$(uv run python -c \
  'import json; print(json.load(open(".ses/registry-register.json"))["version_id"])')"
SES_INITIAL_SHA256="$(uv run python -c \
  'import json; print(json.load(open(".ses/registry-init.json"))["version_sha256"])')"

uv run ses gate candidate \
  --registry .ses/registry --candidate-bundle .ses/candidate \
  --gate-id gate-manual-001 --fixed-scenario accept \
  --command-id command-gate --measured-at 2026-08-18T10:00:02Z --json

uv run ses registry promote \
  --registry .ses/registry --candidate-id "$SES_CANDIDATE_ID" \
  --command-id command-promote --occurred-at 2026-08-18T10:00:03Z --json

uv run ses registry rollback \
  --registry .ses/registry --target-skill-sha256 "$SES_INITIAL_SHA256" \
  --command-id command-rollback --occurred-at 2026-08-18T10:00:04Z --json

uv run ses registry audit --registry .ses/registry --json
```

fixed rejection 会返回退出码 1，但 decision 和 Registry event 已持久化。你只能对通过 Gate 的
candidate 运行 `ses registry promote`；`rollback` 需要一个曾经成为 current 的 verified
Skill hash。

运行本课测试：

```bash
uv run pytest course/ch09-gate-and-govern-versions/tests
```

固定审计产物位于：

- `artifacts/fixed-accept-promote-rollback/`：接受、提升、回滚共五条事件；
- `artifacts/fixed-rejection/`：平局拒绝，candidate 仍保留在 Registry。

你可以调用 `SkillRegistry(path).audit()` 重放并验证它们。所有固定产物都标记为
`synthetic_offline`。

维护者可以从空目录重新生成参考产物：

```bash
uv run python course/ch09-gate-and-govern-versions/scripts/generate_fixed_audit.py
```

生成器只打开上面列出的固定 parent、失败 evidence 和 selection lock。它不会扫描或读取
final，也不会读取环境变量、Key 或调用网络。
