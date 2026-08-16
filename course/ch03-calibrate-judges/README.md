# 第 3 课：校准证据式 AI Judges

## 困惑

模型 Judge 会给出流畅的理由，但流畅不等于可靠。它可能把商品价格、退款额、运费、手续费和折扣误当成同一种金额，也可能在没有最终回复时照样判通过。

## 方法

你先用确定性 extractor 整理 StateDiff、工具时间线、具名金额组件和消息索引。然后，你分别执行 Rubric LLM Judge 与只读 Agent Judge。最后，你把它们产生的 AssertionResult 和版本化人工标签对比。

## 业界做法

成熟评测系统会把可计算事实留给代码，把语义判断留给模型。它们会锁定 rubric、prompt、extractor、模型配置和输出协议，并保存原始响应。这样，你才能解释历史分数为什么变化。

## 关键 insight

not_evaluated 不是失败。它表示证据不足。error 也不是任务失败；它表示 Judge 的输入、输出、证据引用、权限或运行过程坏了。

LLM Judge 做一次直接的 rubric 判定。Agent Judge 先报告它检查过的证据分区，再给结论。两者使用不同协议，所以它们的行为差异可以审计。

## Starter

starter/ 留下四个明确缺口：

- evidence_extractor.py：抽取状态事实、工具时间线、具名金额关系和关键消息。
- llm_judge.py：执行单次、无重试的严格 JSON rubric 判定。
- agent_judge.py：在无 MCP、无 Shop、无写工具的独立 workspace 中检查证据。
- calibration.py：实际运行两个 Judge，再把 canonical assertions 与人工标签对比。

solution/ 提供对应实现。Starter 不包含答案。

## 实现任务

1. 只比较应当相等的金额关系。确认金额、政策计算退款、最终退款和状态退款应一致；费用和折扣单独记录。
2. 限制 evidence reference，只允许 state_diff_facts、tool_timeline、amount_reconciliation 和 key_messages。
3. 为两个 Judge 设计不同、可验证的输出协议。
4. 记录模型 ID、模型锁版本和模型配置 hash。任何模型变化都必须改变 protocol hash。
5. 运行固定离线响应，而不是在 fixture 中直接填写 llm_status 或 agent_status。
6. 只有两个固定协议真实执行后，才允许 agreement artifact 写 measured=true。

## 测试

    uv run pytest course/ch03-calibrate-judges/tests

测试确认 starter 在四个学习点失败、solution 暴露完整实现，并验证签入 artifact 等于固定离线协议的实际输出。

## 对照产物

agreement-experiment.json 保存 4 条人工复核记录的实测结果：

- Rubric LLM Judge：2 / 4 = 0.50
- Evidence Agent Judge：3 / 4 = 0.75

artifact 同时保存人工标签版本、原始固定响应、evidence hash，以及 rubric、prompt、extractor、模型配置和协议 hash。它明确写明 live_model_measured=false。这些数字只描述当前固定数据与协议，不能代表 live 模型准确率。

## 拓展阅读

- 阅读 docs/specs/03-evaluation-judges.md 的 Implementation Decisions。回答：哪些事实必须由脚本计算？
- 阅读 docs/specs/10-cross-module-contracts.md 的 Serialization Rules。回答：为什么模型配置变化必须产生新协议 hash？
- 阅读本课 Agent Judge 的隔离测试。回答：工具调用尝试为什么属于 Judge error，而不是任务 fail？

## 预算

本课默认使用 FakeEngine 重放原始固定响应，费用为 0。只有你明确授权后，才能单独运行 live smoke；live 结果必须生成新的 artifact，不能覆盖本课离线测量。
