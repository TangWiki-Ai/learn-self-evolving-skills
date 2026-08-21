# 评测 Spec

## 目标

Agent 可能在回复中声称已经退款，但没有调用工具或改变订单状态。本项目不采信模型自评。它把运行事件转换为 Trace，再用环境终态和工具时间线判分。

## 当前实现

- Trace 按原始顺序保存消息、工具调用、工具结果、退出状态和用量。关键事件畸形时，解析器返回明确错误。
- State Judge 比较运行前后 snapshot 与确定性预期，逐项记录实际值、预期值和 StateDiff。
- Rule Judge 检查工具是否调用、次数、参数、顺序和禁止调用。
- 每条断言都带结构化 evidence reference。证据不足、Judge 错误和 Agent 失败使用不同状态。
- 聚合使用 failure-first：必要断言失败时，其他通过不能覆盖它。
- 报告只读取已保存的判分记录，不在渲染时重新判断。

## 测试

- stream-json fixtures 覆盖文本分块、并行工具、非零退出、截断和未知事件。
- State Judge 覆盖缺失变化、多余变化、金额错误和正确终态。
- Rule Judge 覆盖调用存在、次数、参数、顺序和禁止调用。
- 合约测试验证 canonical JSON、evidence reference、错误分类和不可变记录。

## 不做什么

- 不用单一 LLM 分数替代环境和规则证据。
- 不让 Judge 访问 shop 写工具、Skill 修改工具或凭据。
- 不把一次通过解释为生产可靠性。
