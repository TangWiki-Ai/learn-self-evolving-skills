# 教学类 Skill 与 Agent 指令文件研究

日期：2026-08-22

## 结论

这个仓库需要把三类职责分开：

1. `CLAUDE.md` / `AGENTS.md` 负责让 Agent 进入仓库后知道项目规则和入口。
2. instructor Skill 负责主动教学、提问、恢复和 8 个站点的教学节奏。
3. `ses journey` 负责可验证的安装后检查、Provider、Key 边界、运行和证据状态。

教学类 Skill 的共同特点不是“写一篇长教程”，而是把教学做成一个有状态的交互流程：识别用户处于新建、恢复还是继续学习；每次只推进一个小步骤；用可运行的例子或仓库证据让用户观察；最后用明确的完成条件收口。

## 官方 Skill 设计规律

Anthropic 的 Agent Skills 文档把 Skill 分成渐进加载的三层：frontmatter 元数据、触发后加载的 `SKILL.md`、按需读取的 references / scripts / assets。Skill 的 description 需要同时写清“能做什么”和“什么时候触发”；正文负责执行流程，不应该把所有参考资料都塞进去。官方还建议把主体控制在约 500 行以内，并把大块资料移到按需加载的文件中。

来源：

- [Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
- [Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- [Anthropic skill-creator/SKILL.md](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md)

对本项目的直接影响：

- frontmatter 应覆盖“拉取后安装、项目介绍、开始/恢复学习、询问下一步、解释证据”这些真实触发语境。
- 正文应该是路由器和状态机，而不是重复 README 的项目介绍。
- 每个站点的详细教学仍应放在现有 `stations/station-N.md` 中，当前 Skill 只读取当前站点。
- API Key 的绝不能进入聊天、文件或状态，这条应保留为 Skill 的硬安全规则。

## 公开教学类 Skill 的做法

### 1. `agent-teacher`

这个公开 Skill 把“教学”和“修 Bug”分开。它只在用户想建立概念模型时触发；如果用户要修代码，它让位给调试流程。它把一次课组织成：直觉、可运行的小例子、代码 walkthrough、容易踩的坑、下一步方向；同时要求跟随用户语言，并默认把课程放在对话中，而不是自动写成长文档。

来源：[JackyYang258/agent-teacher/SKILL.md](https://github.com/JackyYang258/agent-teacher/blob/main/SKILL.md)

适合本项目的部分：

- 明确触发和不触发条件。
- 每次教学先给可观察对象，再解释抽象概念。
- 每个站点结束后给下一步，而不是一次性讲完 8 个站点。
- 用户要“代做”时可以切换到执行模式，但要说明代替用户做了什么判断。

### 2. Matt Pocock 的 `teach`

这个 Skill 把当前目录当成教学工作区，用 `MISSION.md` 保存学习动机，用 `learning-records/` 保存关键学习记录，用 `lessons/` 保存独立课程，用 `progress.md` 保存阶段进度。它把新建、列出和恢复学习轨道分成不同路由，并要求在新主题开始前先确认动机、时间预算和范围。

来源：[opencode-skills-collection/bundled-skills/teach/SKILL.md](https://github.com/FrancoStino/opencode-skills-collection/blob/main/bundled-skills/teach/SKILL.md)

适合本项目的部分：

- 把“新用户”“恢复用户”“当前站点继续”作为不同分支。
- 用持久状态而不是聊天上下文判断用户走到哪里。
- 让用户先确认投入和范围，避免 Agent 自己决定完整学习计划。

不应直接照搬的部分：本项目已经有 `.ses/status.json` 和 8 站 canonical 状态，不需要再引入一套平行的 `progress.md` 作为运行真相。教学记录可以补充，但不能替代 Journey 状态。

### 3. `/learn` 类型的学习轨道 Skill

公开的 `/learn` Skill 把学习分成 LIST、RESUME、NEW 三种模式：没有参数时列出现有轨道；命中已有 slug 时恢复；新主题先确认，再询问动机、时间和范围，创建 `progress.md` 后进入学习阶段。它还使用间隔复习问题和 session log。

来源：[Claude Code /learn skill](https://gist.github.com/chr1stophe/22c62c92a0129a5f8f0c72263e3674ea)

适合本项目的部分：

- 路由条件应写出来，不要让 Agent 自己猜“这是新建还是恢复”。
- “问一个确认问题 → 做一个动作 → 写状态 → 报告下一步”比一次性输出长教程稳定。
- 课程可以增加短的复盘问题，但复盘不应阻塞安装和运行前置检查。

## `AGENTS.md` 与 `CLAUDE.md` 的区别

它们属于同一类“Agent 指令文件”，但不是同一个文件，也不是同一个消费者。

### `AGENTS.md`

`AGENTS.md` 是 OpenAI Codex 使用的仓库指令约定。它通常描述仓库范围、代码规则、测试命令、架构约束和安全边界。它可以在目录树中分层出现，更深目录的指令可以覆盖上层规则。

来源：[OpenAI Introducing Codex — AGENTS.md spec](https://openai.com/index/introducing-codex/)

### `CLAUDE.md`

`CLAUDE.md` 是 Claude Code 的持久项目指令文件。Claude Code 会在会话开始时加载它，并按目录层级发现项目、用户和组织范围的文件。它适合项目架构、常用命令和 Claude 特定工作流。

Anthropic 官方明确说明：Claude Code 读取 `CLAUDE.md`，而不是 `AGENTS.md`；如果仓库已经有 `AGENTS.md`，推荐在 `CLAUDE.md` 中写 `@AGENTS.md`，这样 Claude Code 可以加载同一份共享指令，再在下方追加 Claude 特有规则。

来源：[Claude Code memory — AGENTS.md](https://code.claude.com/docs/zh-CN/memory#agents-md)

### 这个仓库应该怎么放

```text
AGENTS.md       # 跨 Agent / Codex 共享的仓库规则
CLAUDE.md       # @AGENTS.md + Claude Code 的链接拉取与 onboarding 规则
.agents/skills/ # 当前仓库已有的 Agent-agnostic Skill 源文件
```

当前仓库的 `CLAUDE.md` 已改为使用 `@AGENTS.md`。这比在正文里写“请再去读 AGENTS.md”更可靠，因为导入内容会在会话开始时进入上下文。

还要注意一个 Claude Code 兼容性问题：Claude 官方 Skills 文档把项目 Skill 的自动发现目录定义为 `.claude/skills/<skill-name>/SKILL.md`。当前仓库的 instructor Skill 在 `.agents/skills/`，因此它不会依靠 Claude Code 的标准 Skill 自动发现机制；目前只能靠 `CLAUDE.md` 明确要求 Claude 读取它。后续如果希望完全使用 Claude Code 原生自动触发，应增加 `.claude/skills/` 适配层，并保持一个 canonical 源文件，避免两份 Skill 漂移。

来源：[Agent Skills overview — Claude Code locations](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)

## 对当前 instructor Skill 的建议

当前 Skill 已经有“Non-negotiable boundaries、Start or resume、Step router、Teaching posture、Completion”五块，方向是对的。下一步应该把它收敛成下面的结构：

```text
frontmatter                 # 能做什么 + 何时触发
New-user handoff             # 拉取后介绍、确认问题
Credential handoff           # 默认 Provider、Key 命令、重启条件
Journey router               # fresh / resume / attention / complete
Teaching loop                # 观察证据 -> 用户判断 -> 执行 -> 记录下一步
Station router               # 只读当前 station playbook
Failure handling             # 安装失败、Key 缺失、exit code 2、Provider 冲突
Completion                   # 明确的 evidence-facts/index 条件
```

推荐的教学循环：

1. 先显示当前状态和一个具体证据。
2. 问一个小问题，例如“你从这条 evidence 里看到了什么？”
3. 用户判断后再执行下一条命令。
4. 把结果写进现有 `.ses/status.json` 或 Journey 决策文件。
5. 报告完成条件和下一站，不提前讲后面的全部内容。

## 推荐的 Skill 评测用例

教学 Skill 不能只测“能否输出一段介绍”，还要测它是否在正确时间做正确动作：

1. 用户只提供 GitHub 链接并要求拉取、安装：应 clone、安装、介绍、询问确认；不应要 Key，不应启动 live。
2. 用户已在仓库中说“我要学习 Skill 自进化”：应读取现有状态，直接进入 Key 引导或恢复流程。
3. 用户确认“你要开始学习 Skill 自进化吗”：应根据默认或持久 Provider 展示匹配 Key 命令，然后等用户确认已设置。
4. 用户没有设置 Key：应给出可执行命令，不应读取所有环境变量或尝试 fallback。
5. 用户已有 `.ses/status.json` 且 Provider 为 ChatAnywhere：应恢复 ChatAnywhere，不应切回默认 SiliconFlow。
6. 用户说“帮我直接做完”：可以代做，但必须声明代替用户做了哪些判断。
7. 用户说“看一下 README”：不应触发完整教学流程，只完成读取和回答。

这些用例应进入项目自己的 Skill eval，而不是只靠人工试玩。每条用例都应检查触发、命令顺序、是否过早产生付费动作、状态变化和最终用户提示。
