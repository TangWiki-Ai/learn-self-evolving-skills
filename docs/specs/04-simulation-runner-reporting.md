# 模拟、运行与报告 Spec

## Problem Statement

单轮、单 case 的成功不能代表客服 Agent 可靠。真实任务需要模拟用户逐轮补充信息，运行可能中断，模型输出存在方差，费用也会快速累积。如果 evaluator、批量调度、预算和报告各自保存不同状态，学生无法恢复实验、做公平配对或解释一个分数从哪里来。

## Solution

提供受约束的 User Simulator、单 case Evaluator、批量 Runner 和统一 Report Builder。Simulator 只持有私有 intent，只表达用户想要什么，不泄漏操作步骤。Evaluator 管理同一 Claude session 的多轮交互；Runner 管理 case、iteration、预算、恢复和不可变运行记录。Report Builder 从结构化运行产物生成 L1 单次报告、L2 配对报告和 L3 进化报告，所有 HTML 可离线打开并保持可追溯。

## User Stories

1. 作为学习者，我想让模拟用户按 intent 逐轮回答，以便评测多轮客服流程。
2. 作为学习者，我想限制模拟用户不能透露答案或调用写工具，以便测到的是 Agent 能力。
3. 作为学习者，我想继续同一个 Claude session，以便 Agent 保留当前 case 的对话上下文。
4. 作为学习者，我想批量运行一个 split，以便得到可比较的成功率、费用和耗时。
5. 作为学习者，我想重复运行相同 case，以便观察模型方差而不是只看 pass@1。
6. 作为学习者，我想在中断后恢复未完成实验，以便不重复支付已完成 case 的费用。
7. 作为学习者，我想显式重跑某个 iteration，以便新结果不会覆盖历史记录。
8. 作为学习者，我想在运行前看到费用估算和上限，以便控制课程预算。
9. 作为学习者，我想在超限后保留部分结果，以便诊断而不是丢掉已支付的运行。
10. 作为学习者，我想打开 L1 报告查看每个 case 的证据，以便快速定位失败。
11. 作为学习者，我想在 L2 报告中看到同 case 的 baseline 和 Skill 配对，以便区分改进与回归。
12. 作为学习者，我想在 L3 报告中查看版本谱系和进化曲线，以便理解 gate 的长期影响。
13. 作为评审者，我想从报告回到原始 Trace 和 Judge 记录，以便验证图表没有改变含义。
14. 作为无 Key 的读者，我想阅读官方参考报告，以便理解课程产出而不产生费用。

## Implementation Decisions

- Simulator 接收私有 intent、当前对话和允许公开的业务事实，输出下一条用户消息或结束信号。
- Simulator 提示明确限制为表达 want 而不是 how，禁止提及 gold、参考步骤、Judge 规则和隐藏任务结构。
- Simulator 不持有 shop 写工具。需要读取公开订单信息时，通过受限只读接口或由对话中的 Agent 提供。
- Evaluator 负责一个 case 的环境生命周期、Agent session、Simulator 轮次、Trace 累积和最终 Judge 调用。
- 首轮使用新 session，后续轮使用 resume。session 标识只在当前 case 工作区有效，不能跨 case 复用。
- Runner 负责选择 split、iteration 数、并发策略、跳过已完成结果、显式重跑和运行级汇总。
- `resume` 只继续缺失或明确标记可恢复的工作，不把部分 Trace 当成完整通过。显式 rerun 创建新 iteration。
- RunRecord 是 append-only manifest，记录配置 hash、数据版本、模型锁、Skill hash、case 列表、iteration、预算和产物引用。
- 预算控制独立覆盖 case 数、每 case 轮数、总 token 和估算费用。任一上限触发停止，并记录触发原因。
- 运行前估算使用模型锁中的价格快照和保守 token 假设；运行后费用使用实际用量和同一价格版本计算。
- pass^k 根据重复运行定义可靠性。报告同时保留 pass@1、样本数和方差信息，不能只展示一个聚合分数。
- L1 报告包含运行摘要、逐 case 判定、失败证据、工具时间线、StateDiff 和可折叠 transcript。
- L2 报告只比较同 case、同 iteration 策略、同模型协议下的 fresh runs，展示翻转、回退、分数分布和成本差异。
- L3 报告消费 registry 和 gate 记录，展示接受与拒绝分支、跨版本分数与成本曲线、final 结果。
- HTML 报告为自包含静态文件，不依赖远程脚本、字体或 CDN。单文件目标小于 2 MB，超限时优先压缩或外置完整 transcript。
- 报告层只读结构化记录，不重新判分。任何报告计算指标都必须能回溯到输入记录和公式版本。
- 公开参考 run 明确标记模型、日期、数据版本、是否实测以及不可与学生当前环境直接等同的限制。

## Testing Decisions

- Simulator 契约测试验证只输出用户消息或结束信号，并用对抗 intent 检查答案泄漏和越权工具。
- Evaluator CLI 测试使用 fake engine 和 fake simulator 跑完整多轮 case，覆盖正常结束、轮数超限、Agent 错误和 Simulator 错误。
- Resume 测试从不同中断点恢复，断言已完成 case 不重跑、部分 case 状态清晰、新 iteration 不覆盖旧产物。
- 预算测试分别触发 case、轮数、token 和费用上限，验证停止顺序和部分 Trace 持久化。
- 配对测试拒绝 case 集、模型协议、Skill hash 或 iteration 不兼容的比较。
- 报告数据测试验证指标、pass^k、翻转分类、成本和 lineage 映射；渲染测试验证关键内容、可访问结构和无外部网络依赖。
- HTML 体积测试使用代表性完整 run，不能只用空报告证明小于 2 MB。
- 防泄漏测试扫描公开 HTML 和嵌入数据，断言不含凭据、hidden gold、final 私有内容或完整环境路径。
- 参考 run 测试验证 manifest 和报告中所有版本、费用与 measured/estimated 标记一致。

## Out of Scope

- 实时 Web dashboard、服务端数据库、用户账户和远程任务队列。
- 用 Simulator 分数代替环境和 Judge 判定。
- 在报告渲染时调用模型补写解释。
- 自动并发到无法保证预算或环境隔离的规模。
- 把历史缓存作为 baseline、候选或 final 的 fresh rollout。

## Further Notes

- L1、L2、L3 是同一记录模型的不同视图，不应建立三套互不兼容的报告数据。
- Simulator 治理属于给定脚手架；学生负责把它正确接入 Evaluator 和 Runner。
- 报告中必须区分 Agent fail、Judge error、infrastructure error、budget stop 和 not evaluated。
