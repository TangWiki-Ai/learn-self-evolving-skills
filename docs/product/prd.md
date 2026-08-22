# learn-self-evolving-skills — 项目定义

日期：2026-08-22

## 一句话定位

这是一个用可执行评测改进 Agent Skill 的开源实战仓库。

它解决一个具体问题：几段对话看起来更好，不代表问题已经修复，也不能证明没有回归。你在隔离的客服退货沙盒中修改一份 Skill，再用目标回放和完整回归决定是否发布。

## 适合谁

你需要：

- 写过一个能调用工具的 Agent；
- 理解 MCP 的基本概念；
- 会使用 coding agent 和 shell。

你不需要实现评测引擎、Judge、Shop MCP 或 Runner。

## 8 个步骤

| 步骤 | 任务 | 主要输出 |
| --- | --- | --- |
| 0 | 运行 15 个用例的基线 | 基线报告与 no-Skill 对照 |
| 1 | 选择失败用例 | 选择记录 |
| 2 | 判断问题来自环境、用例还是 Skill | 归因记录 |
| 3 | 定位 Skill 中的问题 | 诊断记录 |
| 4 | 做最小修改 | 候选快照、diff、静态检查 |
| 5 | 目标回放与完整回归 | 两道回归检查 |
| 6 | 发布、暂缓或演练本地回滚恢复 | 版本时间线 |
| 7 | 核对输出 | `evidence-facts.json` 与 `evidence-index.json` |

步骤 7 还可以生成项目说明、复盘问题和概念清单。这些是可选文件，不定义项目是否完成。

## 核心规则

- 学习者路径只使用 live Claude Code。`fixed` 只供离线 CI，不能当作 live 成绩。
- 新 workspace 通过 `ses journey start` 使用 `ses.json` 中的默认 Provider；手动运行底层 `station` 命令时仍必须显式传入 `siliconflow` 或 `chatanywhere`。恢复时不能切换 Provider 或模型锁。
- 失败必须来自实际运行。系统不制造失败，也不保证一次修改会提升。
- 只有归因到 Skill 的目标才进入修改与目标回放。
- 目标回放全部通过后，系统才运行完整 15-case 回归。
- Gate 接受要求候选确有运行时变化、case 集完整、目标仍通过，且原先通过的用例 `pass→fail = 0`。
- 回滚是本地 `v1 → v0 → v1` 恢复演练，不是生产部署或流量切换。
- 所有对外数字都要附带 sandbox、模式、Provider、模型锁和证据完整性。

## 系统边界

```text
instructor Skill → ses journey → Claude Code + Shop MCP
                         ↓
             Trace + StateDiff + deterministic Judges
                         ↓
             .ses/status.json + reports + evidence
                         ↓
                  read-only dashboard
```

- instructor Skill 只保留在 `.agents/skills/self-evolving-skill-instructor/`。
- Claude Code 入口负责从仓库链接完成安全拉取、依赖安装和项目介绍，再把学习引导交给 instructor Skill。
- `ses` 顶层只公开 `journey`。内部模块不作为独立产品命令。
- 本地看板只读 `.ses/status.json` 和登记过的输出，不执行命令、不读取 Key、不访问外网。
- 每个 case 使用独立工作区、Claude 配置和订单状态。
- 受测 Skill 只安装 manifest 声明的 `SKILL.md` 与 `references/`。
- Judge 只读取 Trace、工具时间线和环境状态，不采信模型自评。

## Provider 与费用

- SiliconFlow 使用 `SILICONFLOW_API_KEY` 和 `models.lock.json`。
- ChatAnywhere 使用 `CHATANYWHERE_API_KEY` 和 `models.chatanywhere.lock.json`。
- 系统不根据已有 Key 自动选择 Provider，也不做跨 Provider fallback。
- `claude_code_estimate` 是 Claude Code 估算，不是 Provider 账单。
- 无可靠费用时记录 `unavailable` 与 `cost_complete=false`，不能显示为零。
- `synthetic_ci` 只表示 fixed CI。

## 数据边界

- STATE-Bench 提供可执行客服任务与订单环境。
- ABCD 提供角色扮演对话中的表达和意图素材。
- tau2-bench 提供固定的去重与难度信号。
- 这些都是 benchmark 或角色扮演数据，不是生产日志。
- 当前 develop catalog 包含 15 个用例。运行时只读取 `data/testset/ticket07/generated/` 中由 manifest 引用的文件。
- 上游 LICENSE、固定 commit 和 checksum 保留在 `data/upstream/`。

## 仓库结构

```text
.
├── .agents/skills/self-evolving-skill-instructor/  # 讲师与 8 个步骤
├── data/                                            # 固定来源与 15-case catalog
├── fixtures/seed/journey/                           # fixed CI 的 no-Skill 样本
├── fixtures/seed/skill/v0/                          # fixed CI 的初始 Skill
├── src/ses/
│   ├── cli/  journey/  dashboard/                   # 产品入口、状态与看板
│   ├── foundation/  engines/                        # 配置、隔离与 Claude Code
│   ├── shop/  simulation/                           # 客服环境、MCP 与用户模拟
│   ├── evaluation/  evaluator/  runner/             # Trace、Judge 与执行
│   ├── skills/                                      # Skill 安装与静态检查
│   ├── reporting/                                   # 基线与逐 case HTML
│   └── contracts/                                   # 当前运行记录
├── tests/                                           # 离线测试与显式 live smoke
├── scripts/activate_reviewed_assets.py              # 签署后激活首发输入
└── docs/                                            # 当前产品和系统规格
```

## 发布前检查

1. 在干净 workspace 跑通 fixed 8 步路径。
2. Owner 先复核 v0 Skill 与 15 条 develop 用例，签署资产激活决定，再运行 `scripts/activate_reviewed_assets.py`。
3. 资产激活后，分别用 SiliconFlow 与 ChatAnywhere 跑 live Journey；代表性组件 smoke 不能代替完整路径。
4. 验证 Provider 选择、模型锁、凭据隔离和费用语义。
5. 验证 dashboard 的只读、路径与 symlink 边界。
6. 构建 clean wheel，并确认它只暴露 `ses journey` 和当前运行模块。
7. 运行 Ruff、mypy 和完整 pytest。
8. Owner 填完并最终签署 [`human-review-packet.md`](../release/human-review-packet.md)。未签署前，仓库不能声称已完成正式首发验收。

## 不做什么

- 不自动生成或自动重写 Skill。
- 不提供 selection/final 留出集、自动进化循环、Registry 或 Portfolio 系统。
- 不提供远程队列、Web SaaS、多人账户或云端控制面。
- 不训练模型，不修改 Claude Code，也不实现通用 Agent 框架。
- 不承诺固定费用、必然提升、零缺陷或生产有效性。
