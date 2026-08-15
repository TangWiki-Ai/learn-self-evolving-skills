# 系统与模块规格

这些文档把产品需求拆成稳定的系统边界。课程章节可以跨模块组合，但实现、测试和后续 Provider 扩展都以这些模块契约为准。GitHub 上的 [spec tracking issue](https://github.com/TangWiki-Ai/learn-self-evolving-skills/issues/13) 提供统一入口。

## 规格索引

| 规格 | 负责范围 |
| --- | --- |
| [00 系统总览](00-system-overview.md) | 端到端目标、核心约束、角色、模块关系和全局验收 |
| [01 基础运行时](01-foundation-runtime.md) | 配置、凭据、数据获取、工作区隔离、Engine 和 doctor |
| [02 电商环境与 MCP](02-shop-environment.md) | 订单状态、政策 oracle、工具、MCP server、snapshot 和 StateDiff |
| [03 评测与 Judges](03-evaluation-judges.md) | Trace、expect、State/Rule/LLM/Agent Judge 和证据模型 |
| [04 模拟、运行与报告](04-simulation-runner-reporting.md) | 用户模拟器、批量执行、预算、恢复、L1/L2/L3 报告 |
| [05 测试集流水线](05-testset-pipeline.md) | 清洗、聚类、分层、变体、校准、切分和数据可见性 |
| [06 Skill 创建与触发](06-skill-creation-triggering.md) | Skill 安装、Creator、静态门、触发评测和 v0 对照 |
| [07 进化与版本治理](07-evolution-governance.md) | 失败卡片、结构化补丁、selection gate、注册表和回滚 |
| [08 自动进化与作品集](08-automation-portfolio.md) | 有界自动循环、final 纪律、L3 报告和 portfolio |
| [09 课程交付](09-course-delivery.md) | 10 课编排、starter/solution/tests、指标、预算和发布验收 |
| [10 跨模块契约](10-cross-module-contracts.md) | 记录归属、序列化不变量、版本与 contract 变更协议 |

## 模块关系

```text
Foundation Runtime -----> Shop Environment -----> Evaluation & Judges
       |                         |                         |
       +-------------------------+-----------> Simulation/Runner/Reports
       |                                                   |
       +----> Testset Pipeline ----> Skill Creation -------+
                                          |
                                          v
                              Evolution & Governance
                                          |
                                          v
                              Automation & Portfolio

Course Delivery consumes every module and packages the learner journey.
```

Cross-module Contracts 约束所有模块交换的持久记录，但不承载业务实现。

并行开发不要按“一人一份完整 spec”直接开工。具体波次、文件所有权和 handoff 见[多 Agent 并行实施](../development/parallel-implementation.md)。

## 测试边界

系统把 `ses` CLI 当作主要验收边界。默认测试在临时工作区使用固定数据和 fake engine，从命令输入一直验证到结构化结果与 HTML 产物。政策、状态差异、判分、补丁和 gate 等确定性逻辑使用更窄的单元测试。真实模型调用只通过显式 smoke 测试运行，CI 不读取付费凭据。

## 变更规则

- 修改跨模块契约时，先更新系统总览和相关模块 spec，再更新 tickets。
- 新 Provider 必须复用 Engine 边界，不能把供应商细节泄漏到 evaluator、judge 或课程练习。
- 修改数据切分或可见性规则时，必须重新审查防泄漏测试和 final 纪律。
- spec 只记录稳定行为和决策，不绑定容易变化的源码路径。
