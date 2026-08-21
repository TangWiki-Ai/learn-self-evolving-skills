# 课程交付 Spec

## Problem Statement

完整的自进化引擎不等于完整的学习体验。学习者需要在约一天内看懂证据、做出关键判断并跑完一次真实闭环，而不是重写 Python 管道。课程还必须区分 live 证据、CI 合成证据和不可获得的费用数据，避免把固定 fixture、模型估算或缺失费用写成真实 Provider 成绩。

教学入口也必须保持单一。讲师、命令、进度和报告如果各自维护状态，学习者中断后无法可靠恢复，dashboard 也会与真实运行漂移。

## Solution

把已实现的系统能力编排成站 0–7 共八站的单日 Journey。项目级 instructor Skill 负责讲解、代跑命令和递进提示；`ses journey` 执行现有能力并把状态与证据写入 `.ses/`；本地 dashboard 只读 `.ses/status.json` 和其中列出的产物。

学习者路径始终使用真实 Claude Code 和显式选择的 `siliconflow` 或 `chatanywhere`。`fixed` 模式只作为仓库 CI 的确定性测试缝，不属于学习者路径。旧 `course/` 十课、starter、solution 和每课测试已经删除；其必要种子资产迁入 `fixtures/seed/`。

## User Stories

1. 作为学习者，我想对 coding agent 说“开始学习”，以便由讲师 Skill 带我开始或恢复当前站。
2. 作为学习者，我想在新 live workspace 显式选择实验 Provider，以便系统不会从环境中的 Key 猜测或静默切换 Provider。
3. 作为学习者，我想让讲师处理安装、doctor、命令和 dashboard，以便把时间用于理解证据和做判断。
4. 作为学习者，我想在一张八站地图上看到当前进度和产物，以便中断后继续。
5. 作为学习者，我想从真实失败中选择 bad case，以便分析对象来自当前运行而不是教学脚本。
6. 作为学习者，我想区分环境、case 和 Skill 问题，再定位到具体文本，以便不因单次失败盲目修改 Skill。
7. 作为学习者，我想只做一次最小修复并先回放目标 case，以便验证修复方向。
8. 作为学习者，我想看到目标回放与全量回归两道 Gate，以便知道候选是否治好目标且没有伤害既有通过 case。
9. 作为学习者，我想查看发版与回滚时间线，以便理解版本治理。
10. 作为学习者，我想随时生成当前证据对应的简历、面试准备和概念清单，以便 Gate 未通过时也能如实总结。
11. 作为学习者，我想区分 coding-agent 费用、实验引擎费用、估算和不可用费用，以便不把未知数据当成账单。
12. 作为评审者，我想从站 7 产物回到结构化 evidence，以便核查每个数字和结论。

## Implementation Decisions

- 课程定位是经历生成器，不是证书课程。完成状态代表产物已生成，不证明学习者独立完成了每个判断。
- 讲师由 `.agents/skills/self-evolving-skill-instructor/` 下的一份 `SKILL.md` 和站 0–7 playbook 构成。`.claude/skills/` 只提供 Claude Code 的轻量发现入口，不维护第二份正文。
- 讲师默认先提问、再指向证据、再给候选解释，最后才示范。学习者要求代做时，讲师可以代做并明确说明所作判断。
- 每站使用一条 `uv run ses journey station N` 命令。讲师根据学习者决定补充该站参数；学习者无需编写 Python 管道。
- Journey 的 canonical 状态位于 `.ses/status.json`。它记录站点状态、决定引用、产物引用、实验模式、Provider、模型锁哈希、token 和费用来源。恢复时必须沿用已保存的模式与 Provider。
- dashboard 通过 `uv run ses journey dashboard` 启动。它只允许读取状态及已登记产物，不执行命令、不写文件、不读取 Key、不访问外网。
- 新 live workspace 必须显式传入 `--provider siliconflow` 或 `--provider chatanywhere`。系统不根据现有 Key 自动选择 Provider，不在 Provider 之间路由或 fallback。
- SiliconFlow 与 ChatAnywhere 使用各自的模型锁和环境变量。ChatAnywhere 只使用其锁定的 Claude 系列模型；不能复用 SiliconFlow 的 DeepSeek/Qwen 锁。
- `--mode fixed` 只用于仓库 CI。fixed 状态使用 `synthetic_ci` 费用来源，任何 fixed 结果都不能作为 live 模型质量或真实费用证据。
- 课程展示两笔账：coding agent 的订阅或 Key 费用由学习者自己的服务产生，仓库不计量；实验引擎记录 token 和费用来源。系统不实现预算硬停。
- SiliconFlow live 费用来源为 `claude_code_estimate`，它是 Claude Code 估算而非 Provider 账单。ChatAnywhere 不提供可验证的 Provider 费用时，费用来源必须为 `unavailable`、`cost_complete=false`，界面不能显示零费用或推算费用。
- Gate 未通过不会阻塞站 7。站 7 使用现有 evidence 生成草稿或已验证产物，并明确缺失的基线、全量回归、发版或 live 证据。
- Part B 的生产对照正文和精选外链仍待 Owner 终审。讲师只能教授已批准的仓库机制和明确的沙盒边界，不能把待审内容写成正式课程结论。

课程与系统能力映射如下：

| 站 | 简历短语 | 学习者判断 | 系统执行 | 主要产物 |
| --- | --- | --- | --- | --- |
| 0 | Execution & Monitoring | 观察，不下结论 | doctor、15-case v0 baseline、固定五条 no-Skill 对照 | baseline HTML 与执行证据 |
| 1 | Bad Case Mining | 选择要分析的失败 case | 汇总基线失败 | 失败清单与选择记录 |
| 2 | Failure Analysis | 判断环境 / case / Skill 归因 | 校验并保存归因 | 归因分布与决定记录 |
| 3 | Skill Diagnosis | 选择诊断标签和文件位置 | 关联失败、Skill 文本与位置 | 诊断定位视图 |
| 4 | Minimal Refinement | 给出最小修复方案 | 生成候选快照、diff 并运行静态门 | 候选 Skill 与修复 diff |
| 5 | Regression Evaluation | 跟随 Gate、继续收窄或暂缓 | 目标回放 + 全量回归两道 Gate | Gate JSON/HTML 与回归决定 |
| 6 | Version Release & Rollback | 发版、回滚演练或暂缓 | 写入不可变版本时间线 | 发版与回滚证据 |
| 7 | Evidence-backed Portfolio | 核对事实 | 从 evidence 机器填充模板 | 中英简历、面试准备、概念清单与证据索引 |

## Testing Decisions

- 默认测试使用 fixed fixture 和 fake engine，在临时 workspace 跑完整八站，不访问网络、不读取付费 Key。
- fixed 测试必须把模式和费用来源标记为 `fixed` / `synthetic_ci`，站 7 必须将其表述为 CI 合成证据草稿。
- live smoke 只在显式授权、显式 Provider 和匹配凭据下运行。SiliconFlow 与 ChatAnywhere 共用 Engine 合约，但分别验证模型锁、凭据隔离、Model 与 MCP 链路。
- Journey 测试覆盖新 workspace 的 Provider 必选、恢复时 Provider 固定、跨 Provider 切换拒绝和模型锁哈希漂移拒绝。
- dashboard 测试覆盖只读方法、目录穿越、symlink 逃逸、未登记产物和凭据材料拒绝。
- 费用测试覆盖 `claude_code_estimate`、`unavailable` 和 `synthetic_ci` 三种来源。ChatAnywhere 的缺失费用必须贯穿 runner、报告、Journey 和 dashboard，不能在聚合时变成零。
- 两道 Gate 测试分别验证所有目标 case 变绿、完整回归 case 集以及既有通过 case 的 `pass→fail = 0`。
- 站 7 测试从 evidence JSON 校验所有数字，并验证没有完整证据时省略成绩声明而不是补造数字。
- 包发布测试验证 instructor Skill、playbook、Journey 资源和 ChatAnywhere 模型锁随安装包交付；旧 `course/` 不再是发布输入。
- 文档命令和 README 的最短路径必须通过 CLI 集成测试保持一致。

## Out of Scope

- 让学习者重写 Trace、Judge、Runner、Gate 或其他已有 Python 模块。
- starter/solution 链式十课、每课独立测试和无 Key 回放课程。
- 公开教学网站、LMS、证书或监考机制。
- 自动 Provider 选择、跨 Provider fallback、负载均衡或结果等价保证。
- 根据预算自动停止学习者运行，或把估算当成 Provider 最终账单。
- 承诺模型一定产生失败、Gate 一定拒绝或不同时间得到相同分数。
- 在 Owner 终审前发布 Part B 生产对照正文。

## Further Notes

- 旧十课仍可从 Git 历史回查，但当前文档、测试和发布流程不得依赖 `course/`。
- `.ses/` 是本地运行状态和证据目录，不提交 Git。学习者恢复时不应删除它。
- 对外数字必须同时说明 sandbox、实验模式、Provider、模型锁和证据完整性。
