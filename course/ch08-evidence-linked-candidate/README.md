# 第 8 课：生成有证据链接的候选 Skill

本课把失败评测转成可审核的 Failure Card，再用小型结构化补丁生成不可变候选。

流程固定为：

```text
脱敏失败 evidence
  → 固定归因：runtime/environment → case/gold → Judge/Simulator → Skill
  → 六类 Failure Card
  → add / update / delete PatchOperation
  → schema + patch validation + Ticket 08 Static Gate
  → 新的 candidate Skill
```

## 边界

可信分析器读取脱敏失败 fixture，先排除非 Skill 根因，再生成带 Trace/Assertion
引用的 `FailureCardSet`。Updater workspace 只包含这个卡片集、Updater 规范和 accepted
parent 的可安装 Skill 文件。它看不到原始 evidence、源码、凭据、gold、
selection/final 数据、Judge 私有材料或 provider stream。
候选创建永远读取 parent 的 hash，并把结果物化到新的目录；它不会原地修改 parent。

`failure_kinds` 是导出器机械统计的失败 Assertion 类型，不等于语义归因。
`failure_categories` 才是六分类审核结果。真实流程应让轻量模型提出分类，再由人审核；
本地分析器只接受审核后的单一分类，不会根据 hash 或 Assertion 名称猜类别。课程 synthetic
fixture 固定保存这一步的审核结果，因此离线流程仍可复现。

`judge_simulator_health` 是另一项独立审核。导出器总是先写 `not_reviewed`，因为“拿到了
Assertion”不能证明 Judge 或 Simulator 没有漂移。审核者确认协议、rubric 和 Simulator
行为正常后把它标记为 `healthy`；`unhealthy` 或 `not_reviewed` 都不能生成 Skill patch。

`tests/fixtures/evolution/live-failure-evidence.json` 来自 Ticket 08 live paired
artifact 的最小导出。它保留了比较、pair execution、事件日志和 v0 的哈希，删除了
原始 provider stream、绝对路径、订单/客户标识、金额、模型私有内容。这个 live fixture
包含 3 个 `infrastructure_error`，所以分析器拒绝 Skill patch，也不把它们改写成六类
教学失败。

`artifacts/failure-evidence.json` 保存这些引用实际指向的固定 evidence bytes；
`artifacts/synthetic-failure-cards.json` 由这份 fixture 机械生成，不再手写。
`artifacts/evidence-linked-patch.json` 由固定 FakeUpdater 生成。两者和
`artifacts/evidence-linked-patch-list.json` 都明确标记为 synthetic，不能冒充 live
provenance。

## 运行离线 vertical slice

```bash
uv run ses evolve \
  --parent course/ch07-create-v0/artifacts/skill/v0 \
  --evidence tests/fixtures/evolution/synthetic-failure-evidence.json \
  --output .ses/lesson-08 \
  --mode fixed \
  --json
```

这条命令依次生成卡片、调用 FakeUpdater、绑定证据与 parent hash、应用补丁并运行
Static Gate。它把 `failure-evidence.json`、`failure-cards.json`、`patch.json`、
`candidate.json`、`summary.json` 和完整 `skill/` 一次性发布到同一个 bundle；任何一步
失败都不会留下半成品。

把 `--mode fixed` 改为 `--mode live` 后，系统才通过锁定的 Claude Code + SiliconFlow
Creator 模型运行 Updater。模型只能提出 operation、target、content、Failure Card ID、
理由和风险；它不会替代前面的分类审核。程序负责绑定 hash 和 evidence，并继续执行同一套
确定性校验。

## Starter 与 solution

`starter/evolution.py` 保留失败分析、candidate 创建和完整串联的实现缺口；
`solution/evolution.py` 直接调用 `ses.evolution` 的生产逻辑。运行课程测试：

```bash
uv run pytest course/ch08-evidence-linked-candidate/tests
```

## 固定参考产物与成本

你可以直接检查以下四份已提交产物：

- [`failure-evidence.json`](artifacts/failure-evidence.json)：所有 Failure Card 和 PatchOperation 引用共同锁定的 synthetic evidence bytes；
- [`synthetic-failure-cards.json`](artifacts/synthetic-failure-cards.json)：固定失败证据生成的六分类卡片；
- [`evidence-linked-patch.json`](artifacts/evidence-linked-patch.json)：FakeUpdater 提出的证据绑定补丁；
- [`evidence-linked-patch-list.json`](artifacts/evidence-linked-patch-list.json)：本课固定 funnel 的汇总入口。

这些文件全部来自 `fixed/offline` 路径。该路径使用 FakeUpdater，不访问网络、不读取 Key，外部付费调用和 Provider 费用都是 ¥0。这个数字只描述固定课程运行，不是 live measured 成本。仓库没有提交本课 live Updater 的费用或质量结果；你运行 `--mode live` 前必须另设 token 和费用上限，并把实测记录写到独立输出目录。

## 拓展阅读

- [`07-evolution-governance.md`](../../docs/specs/07-evolution-governance.md)：回答 Failure Card、PatchOperation、不可变 parent 和 Gate 之间的职责边界。
- [`10-cross-module-contracts.md`](../../docs/specs/10-cross-module-contracts.md)：回答 evidence hash、candidate hash 与下游 Registry 如何连接，而不泄漏原始失败材料。
