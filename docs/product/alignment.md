# 已确认的产品与架构决策

本文记录 2026-08-15 对齐结果。后续实现以产品需求和本文为准；如果两者冲突，以更新后的显式决策为准。

## 项目边界

- 项目位于独立仓库 `learn-self-evolving-skills`，不修改或嵌入 `skill-up`。
- `skill-up` 只作为评测机制和报告设计的参考，不是运行时依赖。
- 项目公开托管在 GitHub，课程代码使用 Apache-2.0。
- 主产出是学生亲手实现的自进化系统，Skill 只用于证明系统有效。

## 开发顺序

1. Phase 0 只做快速冒烟测试，限制为一个 ticket，目标耗时不超过半天。
2. 冒烟测试只确认数据可获取和读取、Claude Code headless 能通过硅基流动调用模型、MCP 工具可调用、stream-json 可解析。
3. Phase 0 通过后，优先跑通单 case 的完整评测链：数据加载、独立订单环境、MCP、Claude Code、Trace、State/Rule Judge、L1 报告。
4. 随后再加入批量运行、测试集构造、Skill 生成、结构化补丁、门控、回滚、自动进化和 portfolio。
5. 不为 Phase 0 设置统计门槛，不提前做稳定性研究；课程对应阶段再测可靠性、成本和方差。

## 数据边界

- STATE-Bench `customer_support` 固定 commit `5644b183`，负责所有可执行评测。
- ABCD 的退货退款相关子集负责清洗、表达和意图聚类。
- tau2-bench retail 历史轨迹只用于去重和难度分层，不用于重放执行。
- 文档必须把这些数据称为 benchmark 或角色扮演语料，不能称为真实生产日志。
- 仓库提交课程所需的固定小型切片、manifest、SHA256、许可证和来源说明。
- 完整上游数据通过锁版本的下载和切片脚本获取，不直接提交整个上游仓库。

## 模型与运行时

- 首版只实现 Claude Code headless + 硅基流动。
- 主 Agent 和 Creator 使用 DeepSeek 系模型；Simulator 和 Judge 使用 Qwen 系模型。
- 系统保留薄 Engine 边界，但不提前建设通用多 Provider 框架。
- 架构文档必须说明未来如何增加 Provider；只有实测主路径失败时才启用路由兜底。
- 凭据只从环境变量注入，不写入仓库、日志、报告或测试数据。

## 规格与任务管理

- specs 按系统模块拆分，不按 10 节课机械拆分。
- 单独维护课程交付 spec，映射课程、模块、starter、solution、tests 和对照指标。
- specs 在仓库内版本控制，GitHub tracking issue 提供统一入口。
- 开发 tickets 使用 GitHub Issues，一个 ticket 交付一个可演示的纵向切片，并声明真实阻塞关系。

## 测试边界

- 主要验收边界是 `ses` CLI 在临时工作区中的外部行为。
- 默认测试使用固定数据和 fake engine，不消耗 API Key。
- 政策计算、状态差异、判分和 gate 等确定性逻辑使用聚焦的单元测试。
- 真实硅基流动调用属于显式触发的 smoke 测试，不进入默认 CI。
