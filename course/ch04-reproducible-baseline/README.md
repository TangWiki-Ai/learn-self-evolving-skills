# 第 4 课：跑出可恢复的 baseline，并打开 L1 报告

## 困惑

一个 case 跑通一次，不代表 Agent 稳定。批量实验还会中断、超预算。你需要知道哪些结果已经完成，哪些只是没评测，恢复时也不能重复花费。

## 方法

Runner 把 case 和 iteration 组成固定计划。它把每次状态变化追加到 `events.jsonl`，因此恢复只补缺失或可恢复的工作。显式 rerun 创建新 iteration，不覆盖旧结果。预算按固定顺序检查：case、turn、input token、output token、cost。

报告只读取这些记录。它展示 pass@1、pass^k、样本数、逐 case 证据、工具时间线、StateDiff、用量、成本和延迟，但不会重新调用 Judge。

## 关键 insight

把“执行记录”和“报告视图”分开。记录保持不可变，报告随时重建。这样你能解释每个数字来自哪次运行，也能安全恢复。

## Starter

[`starter/baseline.py`](starter/baseline.py) 保留了一个 `NotImplementedError`。你需要实现 `baseline_reliability`：

1. 按 `case_id` 分组，并按 iteration 排序。
2. 忽略 `budget_stop` 和 `not_evaluated`，但保留 `agent_fail`、`judge_error` 和 `infrastructure_error` 作为已评测失败。
3. 计算首个已评测 iteration 的 pass@1。
4. 只有前 k 次都通过时，该 case 才计入 pass^k。

参考实现位于 [`solution/baseline.py`](solution/baseline.py)。

## 运行 baseline

```bash
uv run python -m ses.cli.baseline \
  --run-id run-lesson-4 \
  --iterations 2 \
  --json
```

默认命令只使用 FakeEngine 和 FakeSimulator，不联网，不读取 Key。输出目录中的 `events.jsonl` 是 append-only 记录，`l1.html` 可直接离线打开。

恢复与显式重跑：

```bash
uv run python -m ses.cli.baseline --run-id run-lesson-4 --iterations 2 --resume
uv run python -m ses.cli.baseline --run-id run-lesson-4 --iterations 2 --resume \
  --rerun state-bench-customer-support-2-return-defective-electronics
```

## 测试

```bash
uv run pytest course/ch04-reproducible-baseline/tests
```

## 对照产物

[`baseline-comparison.json`](baseline-comparison.json) 明确区分两种来源：

- `outcome_source` 是本地 FakeEngine fixture 的 measured 结果。
- `live_provider_projection` 是 estimated 说明。我们没有运行 live provider，不能把它写成实测成绩。

这组课程数据用于验证统计逻辑，不代表真实模型表现。live 成本、延迟和成功率需要你显式授权并单独记录。

## 预算停止语义

Runner 在启动 case 前检查 case 上限。case 开始后，Evaluator 先执行 turn 上限。Runner 保存返回的完整或部分记录，再依次检查 input token、output token 和 cost。第一个超限原因成为 `stop_reason`；后续计划项写成 `not_evaluated`。
