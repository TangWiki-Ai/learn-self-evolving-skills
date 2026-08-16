# 第 3 课：校准证据式 AI Judges

## 困惑

模型 Judge 会给出流畅的理由，但流畅不等于可靠。你必须先知道它和人工标签在哪些 case 上一致，在哪些 case 上分歧。

## 方法

先用确定性 extractor 整理 StateDiff、工具时间线、金额对账和消息索引。Rubric LLM Judge 与只读 Agent Judge 只能基于这份证据判定语义断言。随后用固定人工标签计算 confusion matrix、逐条分歧和实际 agreement。

## 关键 insight

`not_evaluated` 不是失败。它表示证据不足。`error` 也不是任务失败；它表示 Judge 的输入、输出、证据引用或运行过程坏了。

## Starter

[`starter/agreement.py`](starter/agreement.py) 保留了一个明确的 `NotImplementedError`。你需要实现 `summarize_agreement`：

1. 逐条比较人工标签与一个 Judge 的结果。
2. 填满包含 `pass`、`fail`、`not_evaluated`、`error` 的 confusion matrix。
3. 返回实际 agreement 和分歧 case ID。
4. 缺预测字段时抛错，不能猜测。

参考实现位于 [`solution/agreement.py`](solution/agreement.py)。

## Agreement experiment

[`agreement-experiment.json`](agreement-experiment.json) 包含 4 条明确标记为人工复核的固定记录。它使用离线固定响应，没有调用 live 模型。对这组 fixture：

- Rubric LLM Judge 的实际 agreement 是 `2 / 4 = 0.50`。
- Evidence Agent Judge 的实际 agreement 是 `3 / 4 = 0.75`。

这些数字只描述这 4 条固定记录。它们不是 PRD 目标，也不能代表 live 模型准确率。

## 测试

```bash
uv run pytest course/ch03-calibrate-judges/tests
```

仓库默认验证 solution，并确认 starter 仍保留本课缺口。

## 只读权限

Agent Judge 的 EngineRequest 使用空的 `allowed_tools`。证据直接放进 prompt；Judge 不持有 Shop 写工具、Skill 修改工具、隐藏答案或参考轨迹。任何工具调用都记为 Judge error。

## 预算

本课默认使用 FakeEngine，费用为 0。只有你显式授权后，才能单独运行 live smoke。
