# 第 4 课：跑出可恢复的 baseline，并打开 L1 报告

## 困惑

一个 case 跑通一次，不代表 Agent 稳定。批量实验还会中断、超预算。你需要知道哪些结果已经完成，哪些只是没评测，恢复时也不能重复花费。

## 方法

Runner 把 case 和 iteration 组成固定计划。它把每次状态变化追加到 `events.jsonl`，因此恢复只补缺失或可恢复的工作。显式 rerun 创建新 iteration，不覆盖旧结果。预算按固定顺序检查：case、turn、input token、output token、cost。

报告只读取这些记录。它展示 pass@1、pass^k、样本数、逐 case 证据、工具时间线、StateDiff、用量、成本和延迟，但不会重新调用 Judge。

## 关键 insight

把“执行记录”和“报告视图”分开。记录保持不可变，报告随时重建。这样你能解释每个数字来自哪次运行，也能安全恢复。

## Starter

[`starter/baseline.py`](starter/baseline.py) 留下三个核心连接点。你需要实现：

1. `evaluate_case`：首轮创建 session，后续轮 resume 同一 session，并在 Simulator 结束后调用 Judge。
2. `run_baseline`：把固定 case 计划逐项交给 Evaluator。
3. `build_l1_report`：从 Runner 记录计算 pass@1 和 pass^k，同时保留逐 case 证据。已采样但不足 k 次的 case 仍进入 pass^k 分母。

参考实现位于 [`solution/baseline.py`](solution/baseline.py)。

## 运行 baseline

```bash
uv run python -m ses.cli.baseline \
  --run-id run-lesson-4 \
  --iterations 2 \
  --json
```

默认命令加载当前可执行 develop catalog，使用 FakeEngine 和 FakeSimulator 跑完整离线 Pipeline，不联网、不读取 Key。每个 case 都有独立 workspace 和 shop；每轮 Trace 会立即写入 `artifacts/`。Simulator 结束后，Evaluator 运行 State Judge 和 Rule Judge。`events.jsonl` 保存 append-only attempt，`l1.html` 可回到 Trace、StateDiff 和 CaseGrade。

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

Runner 在启动下一轮前检查 case、turn、input token、output token 和 cost。Evaluator 每完成一轮就写 Trace 和用量。达到上限后，Runner 保留原 attempt，再追加独立的 `budget_stop`；它不会把已经完成的 Agent/Judge 结果改写成预算状态，也不会为未启动的工作伪造零用量 attempt。
