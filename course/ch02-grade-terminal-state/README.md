# 第 2 课：从终态给一个 case 判分

## 困惑

Agent 说“已经退款”不代表订单真的变了。只看最终回复，你无法区分完成业务、调用失败和只说不做。

## 方法

你先运行固定退货 case，保存 Trace、执行前后快照和 StateDiff。然后用 State Judge 检查业务终态，用 Rule Judge 检查工具参数与顺序。`expect` 在调用 Agent 前拦截缺 fixture、缺工具和错误预算。

## 业界做法

可靠评测优先读取可验证的环境状态和工具证据。模型评价只能补充这些证据，不能替代它们。

## 关键 insight

把“Agent 说了什么”和“系统发生了什么”分开。状态断言失败时，后续文字评价不能把它改成通过。

## Starter

[`starter/baseline.py`](starter/baseline.py) 保留了一个明确的 `NotImplementedError`。你需要统计 `state_grade == "pass"` 的记录数，并返回通过数、总数和通过率。

## 实现任务

1. 运行 `uv run ses run-case --json`，查看 Trace、工具输入输出、StateDiff 和断言。
2. 实现 starter 中的 `state_pass_rate`，不要把 `judge_error` 或 `not_evaluated` 算作通过。
3. 用同一组测试比较你的实现和 [`solution/baseline.py`](solution/baseline.py)。

## 测试

```bash
uv run pytest course/ch02-grade-terminal-state/tests
```

仓库默认检查 solution，并确认 starter 仍停在预期缺口。你完成练习时，将测试目标切换到自己的 starter 实现。

## 对照产物

[`baseline-results.json`](baseline-results.json) 包含 6 条离线练习记录。它明确标记 `measured: false`，不是模型实测成绩。参考结果是 `4 / 6 = 66.67%`；它只用于验证统计逻辑。

## 拓展阅读

- 阅读 `docs/specs/03-evaluation-judges.md` 的确定性 Judge 与失败优先级，回答：为什么 State fail 不能被其他 Judge 覆盖？
- 阅读 `docs/specs/10-cross-module-contracts.md` 的 artifact 规则，回答：为什么证据引用要带 SHA256？

## 预算

本课默认使用 FakeEngine 和本地 Shop MCP，费用为 0。只有显式运行 live smoke 才会读取你的硅流 Key。
