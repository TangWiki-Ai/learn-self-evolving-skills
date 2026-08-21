# 电商环境与 MCP Spec

## Problem Statement

客服 Agent 很容易生成看似正确的文字，却没有执行正确的退货操作。纯文本答案无法证明退款金额、订单状态、政策窗口或工具顺序正确。课程需要一个足够真实但完全可控的业务环境，让相同案例可重放、变体题有确定性 gold、每次运行互不污染，同时不要求学生先开发一套电商后端。

## Solution

提供一个确定性的退货退款环境。环境从 STATE-Bench 固定版本导入任务语义，使用课程维护的订单状态与政策引擎计算允许操作和 gold，暴露一组有明确读写边界的 MCP 工具。每个 case 从固定种子创建新环境；运行前后生成 snapshot 和 StateDiff。环境、MCP server、政策 oracle 和模拟器安全约束属于给定脚手架，学生通过测试和指定阅读理解它们，但不在课程主体中重写。

## User Stories

1. 作为学习者，我想重放同一个退货案例，以便比较不同 Skill 时只改变被测变量。
2. 作为 Agent，我想通过 MCP 查询订单和执行允许的退货操作，以便完成真实状态变更。
3. 作为 Judge，我想读取运行前后 snapshot，以便基于终态而不是语言承诺判分。
4. 作为模拟用户，我想只表达已给定的 intent，以便不替 Agent 调用 shop 工具。
5. 作为 evaluator，我想为每个 case 创建独立订单状态，以便并行或重复运行不会互相影响。
6. 作为维护者，我想锁定工具 schema 和政策版本，以便 Trace 可以解释。
7. 作为学习者，我想在报告中看到可读的 StateDiff 和工具时间线，以便定位失败原因。

## Implementation Decisions

- 订单状态、商品、付款、会员等级、退货窗口、促销与退款结果使用经过验证的领域模型表达。
- 政策引擎是环境和确定性 Judge 共享的 gold oracle。case 不手工填写可计算终态。
- 政策计算是纯确定性操作：相同政策版本、输入订单和请求必须产生相同决策与金额。
- 每个可写工具执行输入校验、政策校验和原子状态变更。失败返回结构化错误，不留下部分写入。
- 工具集合保持明确且有限。工具 schema 带版本，Trace 记录调用时使用的 schema 和政策版本。
- MCP server 只暴露课程定义的 shop 工具，不提供任意文件、shell、数据库或网络访问。
- Shop MCP 只连接受测 Agent。Simulator 在进程内生成用户回复，Judge 只读已保存的 Trace 和状态证据；两者都不连接 Shop MCP。
- CaseEnvironment 接口负责 reset、snapshot、execute 和 close。evaluator 不直接访问底层存储实现。
- 每次运行从固定 fixture 克隆状态，不重用前次运行数据库。run 标识和 iteration 不能影响业务结果。
- Snapshot 使用稳定排序和规范序列化，StateDiff 只包含有业务意义的新增、删除和修改字段。
- StateDiff 同时保留机器可判字段和人类可读摘要，但摘要不参与确定性 Judge 决策。
- STATE-Bench 负责可执行任务语义；课程可以为一致接口做受控适配，但必须记录原任务标识和转换版本。

## Testing Decisions

- 政策引擎使用表驱动测试覆盖会员等级、时间窗口、商品状态、促销组合、退款方式和边界日期。
- 每个工具都有契约测试，覆盖成功、无效输入、政策拒绝、重复调用和原子回滚。
- MCP 集成测试从客户端列出并调用工具，验证 schema、结果和状态变更，而不直接调用底层函数。
- 环境隔离测试并行运行相同 fixture，断言 snapshot 和状态写入互不影响。
- Snapshot 和 StateDiff 测试验证稳定排序、金额精度、时间规范化和无业务变化时的空差异。
- 集成测试断言 Simulator 和 Judge 路径不获得 Shop MCP 工具。
- STATE-Bench 适配契约使用固定小型切片，不从网络动态下载测试数据。

## Out of Scope

- 完整电商平台、支付网关、库存系统、真实客户账户和真实资金操作。
- 退货退款以外的大规模客服意图覆盖。
- 学生在课程主线中重新实现 MCP server、存储层或政策引擎。
- 让 LLM 生成或裁决本可由政策引擎确定的 gold。
- 跨 case 的持久客户记忆和共享订单状态。

## Further Notes

- 当前环境固定到 STATE-Bench commit `5644b1838d96bc4483da29642d058ecaa6f80f7f` 的 `2-return_defective_electronics` 任务。
- 上游共有 11 个客服工具；本项目只暴露当前退货场景需要的 `get_order`、`get_policies` 和 `process_return`。`process_return` 保留“预览后确认”的顺序，并把美元金额转换为最小货币单位。
- 适配层固定 UTC 时间、严格 JSON schema 和每次运行独立状态。它保留所选任务的判分语义，但不实现无关订单和其他八个工具。
- 环境适配应优先保持上游语义；为了课程体验做简化时，要在 manifest 中说明差异。
- 所有金额在领域层使用精确十进制或最小货币单位，不能使用二进制浮点参与 Judge。
