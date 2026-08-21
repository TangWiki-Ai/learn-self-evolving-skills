# 系统总览 Spec

## Problem Statement

会写 Agent Skill 的开发者通常只能凭几段对话判断 Skill 是否变好。他们缺少可执行环境、可靠判分、可追溯测试集和版本门控，因此无法证明一次修改治好了目标问题，也无法发现对原有能力的回归。

仓库已经具备评测、改进和治理能力，Agent 开发者不需要重写这些管道。系统把现有能力组织成 8 个可恢复的步骤，让你专注三类判断：筛选失败用例、完成归因与定位、提出最小修复并根据回归证据取舍。系统还必须准确区分 live 运行、fixed CI、费用估算和缺失费用。

## Solution

提供一个面向 Agent 开发者的独立 Python 仓库和 `ses` CLI，以 STATE-Bench 客服退货沙盒承载可执行的 Skill 改进实战。项目级 instructor Skill 引导站 0–7 对应的 8 个步骤并操作终端；Journey CLI 编排 doctor、baseline、失败分析、候选修改、两道 Gate 和版本发布；本地只读看板展示 `.ses/status.json` 和已登记输出。

你通过 Claude Code 运行 live 评测，并在新 workspace 显式选择 `siliconflow` 或 `chatanywhere`。默认 CI 只使用 fixed fixture 和 fake engine。所有结论都链接到结构化 evidence；站 7 生成证据索引，并可以生成中英项目说明、面试准备和概念清单等辅助文件。没有完整证据时，机器模板保留空缺或草稿状态。

## User Stories

1. 作为学习者，我想从一句“开始学习”进入或恢复当前步骤，以便不用记住内部模块顺序。
2. 作为学习者，我想在本地看板查看 8 个步骤的进度、费用来源和报告链接，以便理解当前状态。
3. 作为学习者，我想在新 live workspace 明确选择 Provider，以便系统不会从环境变量猜测我的意图。
4. 作为学习者，我想运行 15 个可执行客服用例的 v0 baseline，以便获得当前 Skill 的基线结果。
5. 作为学习者，我想从失败轨迹中选择用例并记录归因，以便让修改来自可回查证据。
6. 作为学习者，我想把诊断定位到 Skill 文本和位置，以便控制修改范围。
7. 作为学习者，我想先验证目标用例，再运行完整回归，以便区分“修复目标问题”和“没有引入新问题”。
8. 作为学习者，我想查看候选接受、拒绝、发布和回滚记录，以便理解版本治理。
9. 作为学习者，我想在发布检查（Gate）未通过时仍生成当前事实的总结，以便如实记录未完成工作。
10. 作为学习者，我想知道费用是 Provider 账单、Claude Code 估算、不可用还是 CI 合成值，以便不误读数字。
11. 作为 evaluator，我想从 Engine 获得 Provider 中立的事件和用量，以便 runner 与 judge 不依赖供应商输出格式。
12. 作为测试作者，我想用 fixed fixture 跑完整 Journey，以便默认 CI 不联网、不产生费用、不读取 Key。
13. 作为安全评审者，我想确认 Agent workspace、状态、报告和错误中不存在凭据或隐藏答案。
14. 作为评审者，我想从可选说明文件回到 evidence JSON，以便核对所有数字。

## Implementation Decisions

- 系统使用 Python 3.11+、Pydantic v2、标准库 `argparse`、pytest、mypy 和 Ruff，不引入重型 Agent 框架。
- `ses` CLI 是主要执行边界。业务逻辑位于独立模块；CLI 只解析参数、调用用例并呈现结果。
- Foundation Runtime 负责配置、Provider 模型锁、凭据读取、Engine 调用和 case 工作区隔离。
- Shop Environment 负责固定订单状态、政策 oracle、工具和 MCP server。每个 case 从已知种子创建独立环境。
- Evaluation & Judges 负责 Trace、StateDiff、断言和判定记录。确定性状态与规则证据优先于模型判断。
- Simulation/Runner/Reporting 负责真实多轮执行、批量运行、恢复、用量记录和 HTML 报告。
- Testset Pipeline、Skill Creation、Evolution、Registry 与 Automation 保留为可复用引擎能力。Journey 模块在 8 个步骤中编排这些能力，不要求学习者实现它们。
- Journey 为站 0–7 建立一份 canonical 状态，原子写入 `.ses/status.json`。状态持久化实验模式、Provider、模型锁哈希、进度、决定、产物和用量；已存在 workspace 不允许切换模式、Provider 或模型锁。
- instructor Skill 的正文位于 `.agents/skills/self-evolving-skill-instructor/`，Claude Code 发现入口位于 `.claude/skills/`。本地看板只读状态和 allowlist 输出，不承担教学正文或执行职责。
- live 路径支持 SiliconFlow 与 ChatAnywhere。两者共用 Claude Code Engine 合约，但使用不同模型锁、端点 allowlist 和环境凭据；系统不自动选择、路由或 fallback。
- SiliconFlow 锁定 DeepSeek 主 Agent/Creator 与 Qwen Simulator/Judge。ChatAnywhere 锁定 Claude 系列角色模型。业务逻辑只引用角色，不写死模型标识。
- `fixed` 只作为仓库 CI seam。学习者路径不得使用 fixed 生成可对外声明的成绩。
- 费用来源是证据字段。SiliconFlow live 可记录 `claude_code_estimate`；ChatAnywhere 的 Provider 费用不可验证时记录 `unavailable` 和不完整费用；fixed 记录 `synthetic_ci`。任何聚合层都不能把不可用费用转成零费用。
- 系统不因预算自动停止运行。讲师 token 与实验引擎用量分开说明，仓库只记录后者可获得的证据。
- 站 5 的两道回归检查（Gate）要求目标用例全部通过，并要求完整回归覆盖基线用例且所有既有通过用例保持通过。净提升不能抵消 `pass→fail`。
- 站 7 可以随时运行。产物状态必须区分缺失基线、仅 fixed、缺失完整回归、候选拒绝、未发布和已验证发布。
- 跨模块记录采用 producer-owned contract。消费者导入 canonical schema，不复制相似模型；持久化记录使用规范 JSON、相对 artifact reference 和稳定 hash。
- 数据来源按既有边界分工：STATE-Bench 提供可执行客服沙盒；ABCD 和 tau2-bench 提供固定的 benchmark/角色扮演派生材料。公开文字不能称其为生产日志。
- 旧 `course/` 十课、starter、solution 和每课 tests 已删除。仍被引擎和 CI 使用的固定资产位于 `fixtures/seed/`。
- Part B 生产对照正文仍待 Owner 终审，不属于当前已发布项目能力。

## Testing Decisions

- 默认 CI 从 `ses journey station` 命令运行 fixed 八站路径，验证状态恢复、结构化 evidence、HTML、Gate、发布和站 7 输出，不访问网络。
- fake engine 重放真实形状的流式事件、工具调用、错误和用量，使默认测试覆盖 Engine 消费方，而不冒充 live 模型结果。
- live Provider 测试必须显式启用、显式选择 Provider 并使用匹配凭据。跳过 live smoke 不是成功证据。
- SiliconFlow 与 ChatAnywhere 分别做配置、模型锁、端点、凭据隔离和事件规范化测试；ChatAnywhere 另验证费用始终保持 unavailable 语义。
- Journey 状态测试覆盖原子写入、canonical JSON、恢复、跨 Provider 拒绝、模型锁漂移拒绝和敏感内容拒绝。
- dashboard 测试覆盖 GET/HEAD 只读边界、路径穿越、symlink、未登记文件和状态损坏。
- 政策 oracle、StateDiff、规则优先级、候选 diff 和两道 Gate 使用聚焦的表驱动测试覆盖边界组合。
- 报告与站 7 测试同时验证语义数据和渲染结果，避免只做整页字符串快照。
- 发布检查验证安装包包含 instructor Skill、站点 playbook、Journey 固定资源和两份 Provider 模型锁，同时不依赖已删除的 `course/`。

## Out of Scope

- 通用 Agent 托管平台、Web SaaS、多人账户、远程队列和云端控制面。
- 自动 Provider 选择、跨 Provider fallback、负载均衡和结果等价保证。
- 把 Key 保存到配置、报告、状态、fixtures 或课程自建凭据服务。
- 使用真实企业生产日志或把 benchmark 数据包装成生产数据。
- 训练或微调基础模型、修改 Claude Code 本体、实现通用 Agent 框架。
- starter/solution 十课和让学习者从零实现引擎模块。
- 证书、毕业门槛、预算硬停或保证一次运行必然改进。
- 未经 Owner 终审的 Part B 生产对照内容。

## Further Notes

- specs 描述稳定系统边界；[8 步交付 Spec](09-course-delivery.md) 定义学习者看到的步骤编排。
- 所有对外实验数字都必须同时带 sandbox、模式、Provider、模型锁和证据完整性说明。
