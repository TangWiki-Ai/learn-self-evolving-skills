# learn-self-evolving-skills — 项目定义文档（v5）

日期：2026-08-20

仓库名：`learn-self-evolving-skills`

---

## 0. 一句话与价值主张

> 用约一天跑完一次可追溯的 Skill 自进化闭环。

学习者在 STATE-Bench 客服退货沙盒里运行真实模型，亲手完成失败选择、归因定位、最小修复和回归取舍。结束时，系统根据真实 evidence 生成中英简历段落、面试准备、概念清单和证据索引。

三个支柱：

1. **真实环境**：生产式电商业务流程（STATE-Bench 数据），Agent 真实调用工具、真实写库、按最终状态判分——不是问答 demo。
2. **证据闭环**：每站产生结构化决定或报告，最终文案只引用 evidence JSON 中已存在的事实。
3. **单日 Journey**：instructor Skill 带学习者沿站 0–7 运行已有引擎；本地 dashboard 从 `.ses/status.json` 展示进度和产物。

Journey 主线：

```text
Execution & Monitoring → Bad Case Mining → Failure Analysis
→ Skill Diagnosis → Minimal Refinement → Regression Evaluation
→ Version Release & Rollback → Evidence-backed Portfolio
```

## 1. 定位与差异化

| | learn-claude-code | learn-harness-engineering | **learn-self-evolving-skills** |
| --- | --- | --- | --- |
| 教什么 | 从零手搓 Agent 本体 | 给 Agent 搭工作环境（harness） | **让 Agent 的能力（Skill）随数据自动进化** |
| 学习者做什么 | Agent 循环本身 | 工作区/索引/回路模板 | **基于真实证据做失败选择、归因定位、最小修复和回归取舍** |
| 环境 | 自己搭 | 基础应用给定 | 电商环境、数据包、CLI 骨架给定 |
| 教学法 | 逐版本递进 | 每项目 weak vs strong 对照 | **instructor Skill 引导八站 Journey，每站一类判断与一组证据** |
| 可视化 | 无（纯终端） | Electron 应用充当活前端，对比靠手动 checklist | **只读 dashboard 展示 `.ses/status.json` 与已登记报告** |

借用的经验：learn-harness-engineering 的对照实验纪律与 learn-claude-code 的动手深度。不绑定任何上游框架；SkillOpt、SkillForge、SkillCAT、DSPy、skill-up 和 skill-eval 只作为待终审的延伸材料来源。

## 2. 受众与前置要求

目标受众：

- 已写过至少一个简单 Agent（用过 function calling 或任一 Agent 框架）；
- 理解 MCP 的基本概念（工具即服务）；
- 能使用 coding agent 和 shell；不要求学习者编写本仓库的 Python 管道。

## 3. 交付标准

**主产出是一段可回查证据的真实经历。**

结课验收（全部满足）：

1. 学习者路径使用 live Claude Code 和显式选择的 SiliconFlow 或 ChatAnywhere，不用 fixed fixture 冒充成绩；
2. 站 0 完成 15-case v0 baseline，并保留固定五条 no-Skill 对照样本；
3. 失败选择、归因、诊断、候选 diff、Gate、版本事件和最终文案都能回链到 `.ses/` 下的 evidence；
4. 站 5 同时报告目标回放和全量回归，净提升不能抵消既有通过 case 的 `pass→fail`；
5. 站 7 生成中英简历、面试准备、概念清单和证据索引，没有完整证据时如实标记草稿或缺失；
6. dashboard 只读 `.ses/status.json` 和已登记产物，不执行命令、不读取 Key、不访问外网。

学生简历句：

> 我在 STATE-Bench 客服退货沙盒中运行真实模型，建立可追溯的执行、失败分析、最小修复、目标回放、全量回归 Gate 与版本回滚链路，并用结构化证据核验改动结果。

站 7 导出物：`resume-zh.md`、`resume-en.md`、`interview-prep.md`、`concepts.md`、`evidence-facts.json` 和 `evidence-index.json`。

## 4. 八站 Journey

每站由 instructor Skill 讲解并代跑一条 learner command。学习者不写管道，只对当前 evidence 做判断。

| 站 | 简历短语 | 学习者判断 | 主要产物 |
| --- | --- | --- | --- |
| 0 | Execution & Monitoring | 观察，不下结论 | 15-case v0 baseline + 固定五条 no-Skill 样本 |
| 1 | Bad Case Mining | 选择失败 case | 失败清单与选择记录 |
| 2 | Failure Analysis | 环境 / case / Skill 归因 | 归因分布与决定记录 |
| 3 | Skill Diagnosis | 诊断标签和文件位置 | 诊断定位视图 |
| 4 | Minimal Refinement | 最小修复方案 | 候选快照、diff 与静态门 |
| 5 | Regression Evaluation | 跟随 Gate、继续收窄或暂缓 | 目标回放 + 全量回归两道 Gate |
| 6 | Version Release & Rollback | 发版、回滚演练或暂缓 | 不可变版本时间线 |
| 7 | Evidence-backed Portfolio | 核对事实 | 中英简历、面试准备、概念清单和证据索引 |

Gate 未通过不阻塞站 7。模型没有暴露可立案失败时，Journey 如实记录现状，不制造一次教学性失败或拒收。

## 5. 课程 insight 素材（待 Owner 终审）

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
3. 貌似合理的修改会伤真实表现：门控拒绝 → 回滚是最有戏剧性的教学时刻。

## 6. 展示与前端策略

- **教学入口**：项目级 instructor Skill 读取当前站 playbook，在 coding-agent 终端讲解并代跑命令。
- **活前端**：本地 dashboard 轮询 `.ses/status.json`，展示八站进度、Provider、token、费用来源和已登记产物。
- dashboard 只读，不执行 learner command、不写状态、不读取 Key、不访问外网；教学正文不复制进页面。
- HTML 报告保留为 evidence 产物，由 dashboard 链接。项目不建设公开站或 LMS。

## 7. 引擎与学习者边界

| 引擎负责 | 学习者负责 |
| --- | --- |
| Provider 隔离、模型锁、Claude Code Engine、Shop MCP | 显式选择 Provider，理解 sandbox 边界 |
| Trace、StateDiff、Judges、Runner 和报告 | 选择要分析的失败 case |
| 归因与诊断记录入口 | 判断环境 / case / Skill 归因并定位文本 |
| 候选快照、diff、静态门与两道 Gate | 提出最小修复并根据回归证据取舍 |
| 版本时间线、回滚和机器模板 portfolio | 决定发版/回滚并核对最终事实 |

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
├── README.md                      # 当前学习入口与八站地图
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
│   ├── journey/  dashboard/         # 八站状态、恢复与只读看板
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

当前学习者命令面：

```bash
uv run ses journey station 0
uv run ses journey station 1
uv run ses journey station 2
uv run ses journey station 3
uv run ses journey station 4
uv run ses journey station 5
uv run ses journey station 6
uv run ses journey station 7
uv run ses journey dashboard
```

## 9. 固定数据与证据来源

### 9.1 三个数据源

| 源 | 内容 | License | 角色 |
| --- | --- | --- | --- |
| **STATE-Bench** `customer_support`（固定 commit `5644b183`） | 33 个退货退款任务 + 21 条完整工具轨迹 + 独立订单环境 | MIT | **执行主线**：所有 live 评测都跑在这个环境 |
| **ABCD**（`product_defect` 等子集） | 1,070 段真人扮演退货退款对话（口语、错字、寒暄），自带原文→脱敏双版本与意图标注 | MIT | 固定派生材料：表达与意图来源，不作为生产日志 |
| **tau2-bench** retail 历史轨迹 | 1,824 条 Agent 运行日志（114 任务×16 次，含工具调用+reward） | MIT | 固定派生材料：去重与难度信号，只读、不重放执行 |

数据诚实性：全部为 benchmark/角色扮演数据，课程话术是「生产式业务流程/真人对话语料」，不是「真实生产日志」；报告标注「基于 STATE 数据的课程评测」。

### 9.2 切分（selection/final 锁死，develop 可扩容）

| 数据组 | 数量 | 用途 | Creator/Updater 可见 | Agent 可见 |
| --- | ---: | --- | --- | --- |
| `creator` | 9 | 生成 v0（已审核成功轨迹） | 全部 | 不运行 |
| `develop` | 15 | Journey baseline、失败分析与全量回归 | 失败轨迹 + Judge 证据 | 当前消息/Skill/工具结果 |
| `selection` | 6（**锁死**） | 门控比较 | 只见汇总分 | 同上 |
| `final` | 12（**锁死**） | 最终验收，不参与修改 | 不可见 | 同上 |
| `trigger-eval` | 20 | 触发评测（10 正 10 负） | — | — |

扩容规则：新题只能进 develop；每道新题必须通过「考卷先考自己」三步（环境重放对账 → judge 试判故意对/错答卷 → 人工抽读）；gold 一律由 `shop/policy.py` 计算，不允许人工手填。

程序性检查（脚手架内置，测试覆盖）：切分互斥；Creator 只读 `creator`；Agent workspace 无 Gold/任务要求/参考轨迹；每 case 全新订单环境；Skill 内容不得含 case/顾客/订单 ID。

### 9.3 费用与来源

| 开销 | 来源 | 产品语义 |
| --- | --- | --- |
| 讲师 token | 学习者自己的 coding-agent 订阅或 Key | 仓库无法可靠计量，由学习者查看自己的服务方案 |
| 实验引擎 | Claude Code 经显式选择的 Provider 运行 | dashboard 展示 token、费用完整性和费用来源 |

系统不写未经校准的总价，也不因预算自动停止。`claude_code_estimate` 只是 Claude Code 估算；`unavailable` 表示没有可靠 Provider 费用，不能显示成零；`synthetic_ci` 只描述 fixed CI。

## 10. 课程规则（诚实性）

- live 与 fixed 证据必须分别标记；fixed 结果只能形成 CI 合成证据草稿。
- 站 7 的数字只从 evidence JSON 机器填充，不经 LLM 改写，也不补造缺失成绩。
- sandbox、Provider、模型锁、Gate 状态和证据完整性必须跟随对外结果。
- benchmark 与角色扮演材料不能称为真实生产日志。
- Part B 生产对照正文和精选外链待 Owner 终审，终审前不作为正式课程内容。

## 11. 首发前必须完成的验证清单

| # | 事项 | 产出 |
| --- | --- | --- |
| 1 | fixed 八站 Journey 在干净 workspace 全链路通过 | `.ses/status.json`、报告与站 7 产物 |
| 2 | SiliconFlow live 路径验证模型、MCP、Skill 和 Judge | 显式 live smoke 记录 |
| 3 | ChatAnywhere live 路径验证锁定 Claude 模型、MCP、Skill 和 Judge | 显式 live smoke 记录 |
| 4 | Provider 选择、恢复、模型锁和凭据隔离 fail closed | 配置与集成测试 |
| 5 | ChatAnywhere 费用 unavailable 贯穿 Engine、runner、报告和 dashboard | 费用语义测试 |
| 6 | dashboard 的只读、路径和 symlink 边界通过 | server/render 测试 |
| 7 | instructor Skill、八站 playbook、Journey 资源和双模型锁进入安装包 | clean-wheel 测试 |
| 8 | Ruff、mypy、pytest 与文档命令检查全绿 | 发布检查记录 |

## 12. 运营默认值

- 正文中文主线，README 含英文摘要；
- License：课程代码 Apache-2.0，数据继承上游 MIT 并保留原始 LICENSE、commit、ID 与 SHA256；
- 首发标准：八站 Journey、instructor Skill、dashboard、双 Provider 和 §11 检查全部完成；
- CLI 名 `ses`（可在首发前调整）。

## 13. 与 skill-up 的关系

`skill-up` 只作为机制参考，不是运行时依赖。仓库已经独立实现 headless 执行、Skill 隔离、确定性 preflight、State/Rule/Model Judge、批量运行、版本 Gate 和报告。Journey 只编排本仓库的 canonical 实现，不调用 `skill-up` CLI，也不复制其状态模型。

当前有意不做：多租户、远程队列、Web 后台、多引擎矩阵、自动 Provider 路由和跨 Provider fallback。
