# 已确认的产品与架构决策

本文记录当前产品决策。发生冲突时，以日期更晚的显式决策为准；spec 定义稳定系统边界，PRD 说明当前产品形态。

## 2026-08-20 superseding decision

本节取代 2026-08-15 关于“六部分十课、学习者实现管道、单一 SiliconFlow 主路径、预算硬护栏和无 Key 回放”的决定。

### 产品与 8 个步骤

- 产品面向 Agent 开发者，是基于可执行评测的 Skill 改进实战。你在 STATE-Bench 客服退货沙盒中运行模型，完成基线评测、失败用例筛选、归因定位、最小修复和回归检查。
- 学习路径包含 8 个步骤，内部状态与 CLI 仍按站 0–7 编号：准备与基线 → 筛选失败用例 → 失败归因 → Skill 诊断 → 最小修复 → 回归检查 → 发布与回滚 → 结果整理。
- 引擎已经实现。你不写 Python 管道，只做失败用例筛选、归因与定位、最小修复与回归取舍三类判断；需要时可以让讲师代做。
- 站 5 的发布检查（Gate）只决定候选版本能否发布，不决定练习是否完成。站 7 随时可运行，并按现有 evidence 如实标记缺失或未完成状态。它可以生成中英项目说明、面试准备、概念清单和证据索引；这些是可选用的辅助输出，不定义产品。

### 教学界面与状态

- 你只面对 coding-agent 终端里的 instructor Skill 和本地只读看板。
- instructor Skill 的 canonical 正文位于 `.agents/skills/self-evolving-skill-instructor/`；`.claude/skills/` 只提供发现入口。
- `ses journey` 把进度、Provider、模型锁哈希、token、费用来源、决定和输出引用原子写入 `.ses/status.json`。本地看板只读该状态及其 allowlist 文件，不执行命令、不写文件、不读取 Key、不访问外网。
- 旧 `course/` 十课、starter、solution 和每课 tests 已删除。引擎或 CI 仍需的固定资产迁入 `fixtures/seed/`；Git 历史保留旧内容。

### Provider 与费用

- learner live 路径显式支持 `siliconflow` 与 `chatanywhere`。新 workspace 必须明确选择其一；恢复时沿用已保存的选择。系统不从现有 Key 猜测 Provider，不自动路由或 fallback。
- 两个 Provider 复用 Claude Code Engine 合约，但使用各自的模型锁、端点 allowlist 和环境凭据。SiliconFlow 使用 DeepSeek/Qwen 角色锁；ChatAnywhere 只使用其 Claude 系列角色锁。
- `fixed` 只供仓库 CI，不属于学习者路径，也不能作为 live 成绩或费用证据。
- 费用分两笔：coding agent 的订阅/Key 由学习者自己的服务计费；实验引擎只展示仓库可获得的证据。SiliconFlow 的 `claude_code_estimate` 是估算，不是 Provider 账单；ChatAnywhere 无可靠费用时必须显示 `unavailable` 和不完整费用，不能显示为零。
- 系统只预估和展示费用，不设置预算硬停，也不承诺固定总价。

### 内容与验收

- 每个步骤使用一条 learner command，产生一个主要输出，并聚焦一类判断。站 5 先要求目标用例全部通过，再要求完整回归中既有通过用例的 `pass→fail = 0`。
- 站 7 数字只来自 evidence JSON，不经 LLM 改写；fixed 结果只能生成明确标注的 CI 合成证据草稿。
- Part B 生产对照正文和精选外链仍待 Owner 终审。终审前，讲师不能把这些待审材料表述为已发布内容。
- 默认 CI 使用 fixed fixture 和 fake engine；live smoke 必须显式选择 Provider、使用匹配凭据，并保持默认测试离线。

## 仍然有效的 2026-08-15 基础决定

- 项目位于独立仓库 `learn-self-evolving-skills`，不修改或嵌入 `skill-up`；`skill-up` 只作为机制参考，不是运行时依赖。
- 项目公开托管在 GitHub，代码使用 Apache-2.0。
- STATE-Bench `customer_support` 固定版本负责可执行评测；ABCD 与 tau2-bench 固定派生资料提供表达、意图、去重和难度信号。
- 文档必须把公开数据称为 benchmark 或角色扮演语料，不能称为真实生产日志。
- 仓库提交所需固定切片、manifest、SHA256、许可证和来源说明；凭据只从进程环境读取。
- 主要验收边界是 `ses` CLI 在临时 workspace 中的外部行为。政策、StateDiff、判分和 Gate 使用聚焦单元测试。
- specs 按系统模块拆分，不按站点复制系统实现；GitHub Issues 定义具体任务状态和阻塞关系。
