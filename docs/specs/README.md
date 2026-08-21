# 系统规格

这些文档只描述当前 8 步 Journey 使用的模块。

| 规格 | 负责范围 |
| --- | --- |
| [00 系统总览](00-system-overview.md) | 产品边界、模块关系和全局约束 |
| [01 基础运行时](01-foundation-runtime.md) | 配置、模型锁、凭据、工作区隔离和 Engine |
| [02 电商环境与 MCP](02-shop-environment.md) | 订单状态、政策、工具、snapshot 和 StateDiff |
| [03 评测](03-evaluation-judges.md) | Trace、State Judge、Rule Judge 和证据模型 |
| [04 模拟、运行与报告](04-simulation-runner-reporting.md) | 多轮模拟、批量运行、恢复和 HTML 报告 |
| [05 Skill 检查](05-skill-validation.md) | Skill manifest、安装 allowlist 和静态检查 |
| [06 Journey 交付](06-course-delivery.md) | 8 个步骤、instructor Skill、本地看板和输出 |

```text
Foundation → Shop MCP → Evaluation → Runner → Journey → Dashboard
                              ↑          ↑
                      Skill checks   Simulator
```

默认测试使用 fixed 数据和 fake engine，不访问网络或付费 Provider。学习者只通过 `ses journey` 进入产品；live 运行必须显式选择 Provider。
