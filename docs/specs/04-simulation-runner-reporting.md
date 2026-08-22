# 模拟、运行与报告 Spec

## 目标

单轮对话不能覆盖需要澄清和工具确认的客服任务。运行还可能中断或产生不完整费用。Runner 必须保存可恢复、可核对的记录，而不是只输出一个分数。

## 当前实现

- Simulator 只表达用户 intent，不泄漏参考步骤，也不能调用 shop 写工具。
- Evaluator 在一个 case 内管理环境、Claude session、多轮消息、Trace 和确定性判分。
- 每个 case 使用独立工作区和全新订单状态；不同 case 不共享 session。
- Runner 按 case 和 iteration 追加写入 RunRecord。已有完整结果可以恢复，部分结果不会被当成通过。
- RunRecord 保存数据版本、模型锁、Skill hash、用量、费用完整性、状态和 artifact reference。
- Runner 区分 Agent fail、Simulator error、Judge error、infrastructure error、budget stop 和 not evaluated。
- Journey 生成基线汇总和逐 case HTML。报告包含状态、工具时间线、StateDiff、Trace 链接和费用来源。
- HTML 自包含，不加载远程脚本、字体或 CDN。报告只读结构化记录，不重新判分。

## 测试

- 多轮测试覆盖正常结束、轮数上限、Agent 错误和 Simulator 错误。
- Runner 测试覆盖恢复、重复 iteration、用量累计、费用不完整和 append-only 记录。
- 报告测试验证指标、证据链接、敏感信息过滤和离线 HTML。
- live 测试必须显式授权；默认 CI 只运行 fake engine。

## 不做什么

- 不提供远程队列、用户账户或云端控制面。
- 不用 Simulator 输出替代环境与 Judge 判定。
- 不在报告渲染时调用模型补写解释。
