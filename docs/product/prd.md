# learn-self-evolving-skills — 项目定义文档（v5）

日期：2026-08-20

仓库名：`learn-self-evolving-skills`

---

## 0. 一句话定位

> 面向 Agent 开发者、基于可执行评测的 Skill 改进实战。

你在 STATE-Bench 客服退货沙盒中运行基线评测，筛选失败用例，完成归因定位和最小修复，再用目标回放与全量回归检查验证改动。站 7 可以根据现有 evidence 生成中英项目说明、面试准备和概念清单；这些是可选用的辅助输出，不定义产品。

三个支柱：

1. **可执行环境**：使用 STATE-Bench 的电商客服沙盒，Agent 调用工具、更新隔离状态，系统按最终状态判分。
2. **证据闭环**：每个步骤产生结构化决定或报告，最终文案只引用 evidence JSON 中已存在的事实。
3. **8 个步骤**：instructor Skill 带你沿站 0–7 运行已有引擎；本地看板从 `.ses/status.json` 展示进度和输出文件。

8 个步骤：

```text
准备与基线 → 筛选失败用例 → 失败归因 → Skill 诊断
→ 最小修复 → 回归检查 → 发布与回滚 → 结果整理
```

## 1. 定位与差异化

| | learn-claude-code | learn-harness-engineering | **learn-self-evolving-skills** |
| --- | --- | --- | --- |
| 教什么 | 从零手搓 Agent 本体 | 给 Agent 搭工作环境（harness） | **基于可执行评测和运行证据改进 Agent Skill** |
| 学习者做什么 | Agent 循环本身 | 工作区/索引/回路模板 | **基于运行证据筛选失败用例、归因定位、完成最小修复和回归取舍** |
| 环境 | 自己搭 | 基础应用给定 | 电商环境、数据包、CLI 骨架给定 |
| 教学法 | 逐版本递进 | 每项目 weak vs strong 对照 | **instructor Skill 引导 8 个步骤，每个步骤聚焦一类判断与一组证据** |
| 可视化 | 无（纯终端） | Electron 应用充当活前端，对比靠手动 checklist | **本地只读看板展示 `.ses/status.json` 与已登记报告** |

借用的经验：learn-harness-engineering 的对照实验纪律与 learn-claude-code 的动手深度。不绑定任何上游框架；SkillOpt、SkillForge、SkillCAT、DSPy、skill-up 和 skill-eval 只作为待终审的延伸材料来源。

## 2. 受众与前置要求

目标受众：

- 已写过至少一个简单 Agent（用过 function calling 或任一 Agent 框架）；
- 理解 MCP 的基本概念（工具即服务）；
- 能使用 coding agent 和 shell；不要求学习者编写本仓库的 Python 管道。

## 3. 交付标准

**核心结果是一条可回查的 Skill 修改与验证记录。**

交付验收（全部满足）：

1. 学习者路径使用 live Claude Code 和显式选择的 SiliconFlow 或 ChatAnywhere，不用 fixed fixture 冒充成绩；
2. 站 0 完成 15-case v0 baseline，并保留固定五条 no-Skill 对照样本；
3. 失败选择、归因、诊断、候选 diff、Gate、版本事件和输出文件都能回链到 `.ses/` 下的 evidence；
4. 站 5 同时报告目标回放和全量回归，净提升不能抵消既有通过 case 的 `pass→fail`；
5. 站 7 生成证据事实与索引；可选说明文件没有完整证据时如实标记草稿或缺失；
6. 本地看板只读 `.ses/status.json` 和已登记输出，不执行命令、不读取 Key、不访问外网。

站 7 的核心证据文件是 `evidence-facts.json` 和 `evidence-index.json`。它还会生成可选用的 `resume-zh.md`、`resume-en.md`、`interview-prep.md` 和 `concepts.md`。

## 4. 8 个步骤

每个步骤由 instructor Skill 讲解并代跑一条 learner command。你不写管道，只对当前 evidence 做判断。内部状态和 CLI 仍使用站 0–7 的编号。

| 步骤 | 任务 | 你的判断 | 主要输出 |
| --- | --- | --- | --- |
| 0 | 准备与基线 | 观察，不下结论 | 15 个 v0 基线用例 + 固定五条 no-Skill 样本 |
| 1 | 筛选失败用例 | 选择要分析的失败用例 | 失败清单与选择记录 |
| 2 | 失败归因 | 判断环境、用例或 Skill 问题 | 归因分布与决定记录 |
| 3 | Skill 诊断 | 选择诊断标签和文件位置 | 诊断定位视图 |
| 4 | 最小修复 | 提出最小修复方案 | 候选快照、diff 与静态检查 |
| 5 | 回归检查 | 根据 Gate 结果继续收窄或暂缓 | 目标回放 + 全量回归两道 Gate |
| 6 | 发布与回滚 | 发布、回滚演练或暂缓 | 不可变版本时间线 |
| 7 | 结果整理 | 核对事实 | 证据索引和可选说明文件 |

发布检查（Gate）未通过不阻塞站 7。模型没有暴露可立案失败时，系统如实记录现状，不制造教学性失败或拒收。

## 5. 内容素材（待 Owner 终审）

**评测（P2）**：

1. 能用状态/规则判就不用 LLM judge——环境终态是唯一不会撒谎的证据；
2. 一次通过 ≠ 可靠：报告 pass^k 而非 pass@1，方差本身是被测对象；
3. LLM judge 是待校准的仪器：rubric + 换序 + 人工对齐样本，否则偏差淹没真实差异；
4. 「说了」≠「做了」：关键步骤必须要求工具调用轨迹证据；
5. 模拟用户必须被约束：只给 intent 不给答案 + 防泄漏，否则评的是模拟器而非 Agent。

**测试集（P3）**：

1. 从真实故障的小批量样本起步建 eval，别等「够大的数据集」；
2. 高区分度题目从日志里筛，但打分器必须先被验证；
3. Gold 优先用执行重放（终态比对），别只比对文本；
4. judge 与人类偏好对拍并治理长度偏置，「考卷先考自己」；
5. 自进化系统必须物理隔离训练轨迹与留出任务，并锁定 judge/simulator 协议。

**进化（P5）**：

1. 判分信号越可靠，进化可以越自动越激进；信号越噪，人工卡口越多；
2. 未经验证的合并有害——补丁要在留出集上过门，平局也拒；
3. 貌似合理的修改也可能造成退化；门控拒绝与回滚用于暴露和撤回这类改动。

## 6. 展示与前端策略

- **教学入口**：项目级 instructor Skill 读取当前站 playbook，在 coding-agent 终端讲解并代跑命令。
- **本地看板**：轮询 `.ses/status.json`，展示 8 个步骤的进度、Provider、token、费用来源和已登记输出。
- 本地看板只读，不执行 learner command、不写状态、不读取 Key、不访问外网；教学正文不复制进页面。
- HTML 报告保留为 evidence 输出，由本地看板链接。项目不建设公开站或 LMS。

## 7. 引擎与学习者边界

| 引擎负责 | 学习者负责 |
| --- | --- |
| Provider 隔离、模型锁、Claude Code Engine、Shop MCP | 显式选择 Provider，理解 sandbox 边界 |
| Trace、StateDiff、Judges、Runner 和报告 | 选择要分析的失败 case |
| 归因与诊断记录入口 | 判断环境 / case / Skill 归因并定位文本 |
| 候选快照、diff、静态门与两道 Gate | 提出最小修复并根据回归证据取舍 |
| 版本时间线、回滚和机器输出模板 | 决定发版/回滚并核对最终事实 |

学习者可以要求讲师代做判断；产品记录实际决定，不把参与深度当成认证。

## 8. 技术栈与执行引擎

- **执行引擎**：Claude Code headless（`-p` + stream-json），版本锁定在 `models.lock.json`。
  - Skill 用原生 SKILL.md 自动发现；触发评测检验真实产品行为。
  - **多轮会话**：首轮 `claude --session-id <id> -p ...`，后续轮 `claude --resume <id> -p ...`（skill-up `SessionResumer` 同一模式）。
  - 命令基线：`--settings '{"disableAllHooks":true}' --permission-mode=<mode>`；每 case 独立配置目录：只装被测 Skill，关闭个人 Skill/memory/extensions，不挂载 Gold。
  - **权限差异**：skill-up 在受信沙箱用 `bypassPermissions`；课程不用 yolo/bypass 代替隔离，用受限权限模式 + MCP 工具白名单。
- **Skill 机制**：受测 Skill 安装到隔离的 Claude 配置；项目级 instructor Skill 位于 `.agents/skills/`，`.claude/skills/` 只提供发现入口。
- **模型接入**：live 路径显式支持两种 Anthropic-compatible Provider：
  - SiliconFlow：`SILICONFLOW_API_KEY`，DeepSeek 主 Agent/Creator 与 Qwen Simulator/Judge，使用 `models.lock.json`；
  - ChatAnywhere：`CHATANYWHERE_API_KEY`，只使用锁定的 Claude 系列角色模型，使用 `models.chatanywhere.lock.json`；
  - 新 Journey workspace 必须传 `--provider siliconflow|chatanywhere`。选择写入状态，恢复时不得切换；系统不根据 Key 自动选择，也不跨 Provider 路由或 fallback。
- **实验模式**：learner path 只使用 `live`；`fixed` 只供仓库 CI，不能作为 live 模型或费用证据。
- **费用语义**：SiliconFlow 的 `claude_code_estimate` 只是估算；ChatAnywhere 没有可靠 Provider 费用时必须保持 `unavailable` / `cost_complete=false`；fixed 使用 `synthetic_ci`。
- **语言与依赖**：Python 3.11+，Pydantic v2，pytest；不引入重型 Agent 框架。
- **仓库结构**：

```text
learn-self-evolving-skills/
├── README.md                      # 项目定位、适用人群与最短使用路径
├── pyproject.toml
├── ses.json                       # 全局配置
├── models.lock.json               # 模型与 CLI 版本锁定
├── .agents/skills/self-evolving-skill-instructor/
│   ├── SKILL.md                   # 项目级讲师入口
│   └── stations/                  # station-0.md … station-7.md
├── .claude/skills/self-evolving-skill-instructor/
│   └── SKILL.md                   # Claude Code 轻量发现入口
├── fixtures/seed/
│   ├── journey/                   # 学习路径 fixed CI 种子
│   ├── skill/v0/                  # 受测 v0 Skill
│   ├── run-ticket08-*/            # paired evidence chain
│   └── capstone-shopping-assistant/ # 兼容能力所需固定资产
├── data/
│   ├── upstream/
│   │   ├── state_bench/           # 固定 commit + MIT License
│   │   ├── abcd/                  # product_defect 等子集切片 + MIT License
│   │   └── tau2/                  # retail 轨迹切片（只读挖掘用）+ MIT License
│   ├── skill-v0/                  # Creator seed 与审核绑定
│   └── testset/                   # case、资格与 protected commitments
├── src/ses/
│   ├── cli/                         # 参数解析与呈现
│   ├── contracts/                   # 跨模块版本化记录
│   ├── foundation/  engines/        # 配置、隔离与 Engine adapters
│   ├── journey/  dashboard/         # 8 步状态、恢复与只读看板
│   ├── shop/                        # state、policy、tools、MCP
│   ├── evaluation/                  # Trace、expect、evidence、Judges
│   ├── evaluator/  runner/          # 单 case 与批量编排
│   ├── reporting/                   # L1/L2/L3
│   ├── testset/  skills/            # 测试集流水线与 Skill 生命周期
│   └── evolution/  automation/      # patch、gate、registry、auto-evolve
├── tests/
├── scripts/                       # 数据准备与维护者验证脚本
└── .ses/                          # 本地状态与证据，运行后生成且不提交 Git
```

当前学习者命令骨架：

```bash
uv run ses journey station <0-7> [当前步骤参数]
uv run ses journey dashboard
```

讲师会补齐当前步骤需要的参数：新实时工作目录必须选择 Provider；步骤 1–4 需要你的选择、归因或修改理由；步骤 6 需要发布、回滚或暂缓决定。不要把命令骨架当作可直接执行的完整命令。

## 9. 固定数据与证据来源

### 9.1 三个数据源

| 源 | 内容 | License | 角色 |
| --- | --- | --- | --- |
| **STATE-Bench** `customer_support`（固定 commit `5644b183`） | 33 个退货退款任务 + 21 条完整工具轨迹 + 独立订单环境 | MIT | **执行主线**：所有 live 评测都跑在这个环境 |
| **ABCD**（`product_defect` 等子集） | 1,070 段真人扮演退货退款对话（口语、错字、寒暄），自带原文→脱敏双版本与意图标注 | MIT | 固定派生材料：表达与意图来源，不作为生产日志 |
| **tau2-bench** retail 历史轨迹 | 1,824 条 Agent 运行日志（114 任务×16 次，含工具调用+reward） | MIT | 固定派生材料：去重与难度信号，只读、不重放执行 |

数据诚实性：全部为 benchmark/角色扮演数据，项目文案可以写「生产式业务流程/真人对话语料」，不能写「真实生产日志」；报告标注「基于 STATE 数据的沙盒评测」。

### 9.2 切分（selection/final 锁死，develop 可扩容）

| 数据组 | 数量 | 用途 | Creator/Updater 可见 | Agent 可见 |
| --- | ---: | --- | --- | --- |
| `creator` | 9 | 生成 v0（已审核成功轨迹） | 全部 | 不运行 |
| `develop` | 15 | 8 步基线、失败分析与全量回归 | 失败轨迹 + Judge 证据 | 当前消息/Skill/工具结果 |
| `selection` | 6（**锁死**） | 门控比较 | 只见汇总分 | 同上 |
| `final` | 12（**锁死**） | 最终验收，不参与修改 | 不可见 | 同上 |
| `trigger-eval` | 20 | 触发评测（10 正 10 负） | — | — |

扩容规则：新题只能进 develop；每道新题必须通过「考卷先考自己」三步（环境重放对账 → judge 试判故意对/错答卷 → 人工抽读）；gold 一律由 `shop/policy.py` 计算，不允许人工手填。

程序性检查（脚手架内置，测试覆盖）：切分互斥；Creator 只读 `creator`；Agent workspace 无 Gold/任务要求/参考轨迹；每 case 全新订单环境；Skill 内容不得含 case/顾客/订单 ID。

### 9.3 费用与来源

| 开销 | 来源 | 产品语义 |
| --- | --- | --- |
| 讲师 token | 学习者自己的 coding-agent 订阅或 Key | 仓库无法可靠计量，由学习者查看自己的服务方案 |
| 实验引擎 | Claude Code 经显式选择的 Provider 运行 | 本地看板展示 token、费用完整性和费用来源 |

系统不写未经校准的总价，也不因预算自动停止。`claude_code_estimate` 只是 Claude Code 估算；`unavailable` 表示没有可靠 Provider 费用，不能显示成零；`synthetic_ci` 只描述 fixed CI。

## 10. 证据规则

- live 与 fixed 证据必须分别标记；fixed 结果只能形成 CI 合成证据草稿。
- 站 7 的数字只从 evidence JSON 机器填充，不经 LLM 改写，也不补造缺失成绩。
- sandbox、Provider、模型锁、Gate 状态和证据完整性必须跟随对外结果。
- benchmark 与角色扮演材料不能称为真实生产日志。
- Part B 生产对照正文和精选外链待 Owner 终审，终审前不作为正式课程内容。

## 11. 首发前必须完成的验证清单

| # | 事项 | 产出 |
| --- | --- | --- |
| 1 | fixed 8 步路径在干净 workspace 全链路通过 | `.ses/status.json`、报告与站 7 产物 |
| 2 | SiliconFlow live 路径验证模型、MCP、Skill 和 Judge | 显式 live smoke 记录 |
| 3 | ChatAnywhere live 路径验证锁定 Claude 模型、MCP、Skill 和 Judge | 显式 live smoke 记录 |
| 4 | Provider 选择、恢复、模型锁和凭据隔离 fail closed | 配置与集成测试 |
| 5 | ChatAnywhere 费用 unavailable 贯穿 Engine、runner、报告和 dashboard | 费用语义测试 |
| 6 | dashboard 的只读、路径和 symlink 边界通过 | server/render 测试 |
| 7 | instructor Skill、八站 playbook、Journey 资源和双模型锁进入安装包 | clean-wheel 测试 |
| 8 | Ruff、mypy、pytest 与文档命令检查全绿 | 发布检查记录 |

## 12. 运营默认值

- 正文和 README 以中文为主；命令、状态字段和必要技术名保留英文；
- License：课程代码 Apache-2.0，数据继承上游 MIT 并保留原始 LICENSE、commit、ID 与 SHA256；
- 首发标准：8 个步骤、instructor Skill、本地看板、双 Provider 和 §11 检查全部完成；
- CLI 名 `ses`（可在首发前调整）。

## 13. 与 skill-up 的关系

`skill-up` 只作为机制参考，不是运行时依赖。仓库已经独立实现 headless 执行、Skill 隔离、确定性 preflight、State/Rule/Model Judge、批量运行、版本 Gate 和报告。Journey 只编排本仓库的 canonical 实现，不调用 `skill-up` CLI，也不复制其状态模型。

当前有意不做：多租户、远程队列、Web 后台、多引擎矩阵、自动 Provider 路由和跨 Provider fallback。
