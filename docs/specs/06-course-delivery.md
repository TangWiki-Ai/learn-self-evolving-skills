# 8 步 Skill 改进实战交付 Spec

## Problem Statement

这是面向 Agent 开发者、基于可执行评测的 Skill 改进实战。开发者使用已实现的引擎查看证据、做出关键判断并完成一轮改进，无需重写 Python 管道。项目还必须区分 live 证据、CI 合成证据和不可获得的费用数据，避免把固定 fixture、模型估算或缺失费用写成 Provider 实测成绩。

教学入口也必须保持单一。讲师、命令、进度和报告如果各自维护状态，学习者中断后无法可靠恢复，本地看板也会与实际运行漂移。

## Solution

把已实现的系统能力编排成 8 个步骤，内部状态与 CLI 仍按站 0–7 编号。Claude Code 入口先处理仓库拉取和依赖安装，再把教学交给项目级 instructor Skill；Skill 负责讲解、代跑命令和递进提示；`ses journey` 执行现有能力并把状态与证据写入 `.ses/`；本地看板只读 `.ses/status.json` 和其中列出的输出文件。

学习者路径始终使用真实 Claude Code。新入口使用配置的默认 `siliconflow` 或 `chatanywhere`，底层 `station` 命令要求显式 Provider。`fixed` 模式只作为仓库 CI 的确定性测试缝，不属于学习者路径。旧 `course/` 十课、starter、solution 和每课测试已经删除；其必要种子资产迁入 `fixtures/seed/`。

## User Stories

1. 作为学习者，我想对 coding agent 说“我要学习 Skill 自进化”，以便由讲师 Skill 带我开始或恢复当前步骤。
2. 作为学习者，我想让新 live workspace 使用配置的默认实验 Provider，以便系统不会从环境中的 Key 猜测或静默切换 Provider。
3. 作为学习者，我想让讲师处理安装、doctor、命令和本地看板，以便把时间用于理解证据和做判断。
4. 作为学习者，我想在一张 8 步地图上看到当前进度和输出，以便中断后继续。
5. 作为学习者，我想从当前运行中选择失败用例，以便分析对象来自运行结果而不是教学脚本。
6. 作为学习者，我想区分环境、用例和 Skill 问题，再定位到具体文本，以便不因单次失败盲目修改 Skill。
7. 作为学习者，我想只做一次最小修复并先回放目标用例，以便验证修复方向。
8. 作为学习者，我想看到目标回放与全量回归两道检查（Gate），以便知道候选是否修复目标问题且没有引入新问题。
9. 作为学习者，我想查看发版与回滚时间线，以便理解版本治理。
10. 作为学习者，我想按需生成当前证据对应的项目说明、面试准备和概念清单，以便复盘或对外说明。
11. 作为学习者，我想区分 coding-agent 费用、实验引擎费用、估算和不可用费用，以便不把未知数据当成账单。
12. 作为评审者，我想从站 7 输出文件回到结构化 evidence，以便核查每个数字和结论。
13. 作为新用户，我想把仓库链接交给 Claude Code，让它拉取仓库、安装依赖、介绍项目并询问我是否开始。

## Implementation Decisions

- 产品定位是面向 Agent 开发者、基于可执行评测的 Skill 改进实战。完成状态只表示系统已记录相应步骤和输出，不评价学习者的参与程度。
- 讲师只由 `.agents/skills/self-evolving-skill-instructor/` 下的一份 `SKILL.md` 和站 0–7 playbook 构成；该目录是项目级 instructor 的唯一入口。
- 讲师默认先提问、再指向证据、再给候选解释，最后才示范。学习者要求代做时，讲师可以代做并明确说明所作判断。
- 用户入口使用 `uv run ses journey start`；每个步骤内部仍使用一条 `uv run ses journey station N` 命令。讲师根据学习者决定补充当前 station 参数；学习者无需编写 Python 管道。
- Journey 的 canonical 状态位于 `.ses/status.json`。它记录站点状态、决定引用、产物引用、实验模式、Provider、模型锁哈希、token 和费用来源。恢复时必须沿用已保存的模式与 Provider。
- 本地看板通过 `uv run ses journey dashboard` 启动。它只允许读取状态及已登记输出，不执行命令、不写文件、不读取 Key、不访问外网。
- 新 live workspace 由 `start` 读取 `ses.json` 的 `default_provider`；手动运行 `station` 时必须显式传入 `--provider siliconflow` 或 `--provider chatanywhere`。系统不根据现有 Key 自动选择 Provider，不在 Provider 之间路由或 fallback。
- SiliconFlow 与 ChatAnywhere 使用各自的单一 Agent 模型锁和环境变量。ChatAnywhere 不能复用 SiliconFlow 的 DeepSeek 锁。
- `--mode fixed` 只用于仓库 CI。fixed 状态使用 `synthetic_ci` 费用来源，任何 fixed 结果都不能作为 live 模型质量或真实费用证据。
- 项目展示两笔账：coding agent 的订阅或 Key 费用由学习者自己的服务产生，仓库不计量；实验引擎记录 token 和费用来源。系统不实现预算硬停。
- SiliconFlow live 费用来源为 `claude_code_estimate`，它是 Claude Code 估算而非 Provider 账单。ChatAnywhere 不提供可验证的 Provider 费用时，费用来源必须为 `unavailable`、`cost_complete=false`，界面不能显示零费用或推算费用。
- 发布检查（Gate）未通过不会阻塞站 7。站 7 使用现有 evidence 生成证据索引和可选说明文件，并明确缺失的基线、全量回归、发布或 live 证据。

实战与系统能力映射如下：

| 步骤 | 任务 | 学习者判断 | 系统执行 | 主要输出 |
| --- | --- | --- | --- | --- |
| 0 | 准备与基线 | 观察，不下结论 | doctor、15 个 v0 基线用例、固定五条 no-Skill 对照 | baseline HTML 与执行证据 |
| 1 | 筛选失败用例 | 选择要分析的失败用例 | 汇总基线失败 | 失败清单与选择记录 |
| 2 | 失败归因 | 判断环境、用例或 Skill 问题 | 校验并保存归因 | 归因分布与决定记录 |
| 3 | Skill 诊断 | 选择诊断标签和文件位置 | 关联失败、Skill 文本与位置 | 诊断定位视图 |
| 4 | 最小修复 | 给出最小修复方案 | 生成候选快照、diff 并运行静态检查 | 候选 Skill 与修复 diff |
| 5 | 回归检查 | 根据 Gate 结果继续收窄或暂缓 | 目标回放 + 全量回归两道 Gate | Gate JSON/HTML 与回归决定 |
| 6 | 发布与回滚 | 发布、回滚演练或暂缓 | 写入不可变版本时间线 | 发布与回滚证据 |
| 7 | 结果整理 | 核对事实 | 从 evidence 机器填充模板 | 证据索引和可选说明文件 |

## Testing Decisions

- 默认测试使用 fixed fixture 和 fake engine，在临时 workspace 跑完整八站，不访问网络、不读取付费 Key。
- fixed 测试必须把模式和费用来源标记为 `fixed` / `synthetic_ci`，站 7 必须将其表述为 CI 合成证据草稿。
- live smoke 只在显式授权、显式 Provider 和匹配凭据下运行。SiliconFlow 与 ChatAnywhere 共用 Engine 合约，但分别验证模型锁、凭据隔离、Model 与 MCP 链路。
- Journey 测试覆盖 `start` 的默认 Provider、恢复时 Provider 固定、手动 station 的 Provider 必选、跨 Provider 切换拒绝和模型锁哈希漂移拒绝。
- dashboard 测试覆盖只读方法、目录穿越、symlink 逃逸、未登记产物和凭据材料拒绝。
- 费用测试覆盖 `claude_code_estimate`、`unavailable` 和 `synthetic_ci` 三种来源。ChatAnywhere 的缺失费用必须贯穿 runner、报告、Journey 和 dashboard，不能在聚合时变成零。
- 两道 Gate 测试分别验证所有目标 case 变绿、完整回归 case 集以及既有通过 case 的 `pass→fail = 0`。
- 站 7 测试从 evidence JSON 校验所有数字，并验证没有完整证据时省略成绩声明而不是补造数字。
- instructor Skill、模型锁和评测数据随 `git clone` 获取，不进入 wheel。clean-wheel 测试只验证 `ses journey` CLI 和当前运行模块，并拒绝历史文档和已删功能进入 wheel。
- 文档命令和 README 的最短路径必须通过 CLI 集成测试保持一致。

## Out of Scope

- 让学习者重写 Trace、Judge、Runner、Gate 或其他已有 Python 模块。
- starter/solution 链式十课、每课独立测试和无 Key 回放课程。
- 公开教学网站、LMS、证书或监考机制。
- 根据 Key 存在与否自动选择 Provider、跨 Provider fallback、负载均衡或结果等价保证。
- 根据预算自动停止学习者运行，或把估算当成 Provider 最终账单。
- 承诺模型一定产生失败、Gate 一定拒绝或不同时间得到相同分数。

## Further Notes

- 旧十课仍可从 Git 历史回查，但当前文档、测试和发布流程不得依赖 `course/`。
- `.ses/` 是本地运行状态和证据目录，不提交 Git。学习者恢复时不应删除它。
- 对外数字必须同时说明 sandbox、实验模式、Provider、模型锁和证据完整性。
