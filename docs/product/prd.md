# learn-self-evolving-skills — 项目定义文档（v4）

日期：2026-08-15

仓库名：`learn-self-evolving-skills`

---

## 0. 一句话与价值主张

> 让 Skill 自进化：亲手搭一条 create → eval → evolve → gate 的自进化循环。

从写一个 Skill 入手，到一个**可评测、可进化、可回滚**的整套系统。课程结束时，一个从真实客服轨迹生成的电商退货 Skill，会在你亲手实现的门控下完成多轮自进化，并在 12 道全留出测试题上交出提升数字。

三个支柱：

1. **真实环境**：生产式电商业务流程（STATE-Bench 数据），Agent 真实调用工具、真实写库、按最终状态判分——不是问答 demo。
2. **每课一个对照数字**：每课结束都产出一个 with/without 或 before/after 的可复现数字，学习进度就是实验记录。
3. **全程 ≤ ¥50**：单 Key（硅基流动），DeepSeek 主线 + Qwen 判分，每课附预算表。

课程主线（知识轴）：

```text
P1 看见     裸跑客服 bot ⟷ 装上你生成的 Skill（肉眼对比）→ 拆解 SKILL.md
P2 怎么评   终态判分 → 规则判分 → LLM Judge 校准 → Agent-as-a-Judge → 模拟器与报告
P3 考题哪来 真日志挖题（ABCD/tau2）→ 变体生成 → 考卷先考自己 → 入库
P4 生成     9 条轨迹 → Skill v0 → 触发评测 → 首次定量对照
P5 进化     失败卡片 → 结构化补丁 → 留出集门控 → 回滚
P6 串联     自动进化循环 → final 12 题 → portfolio
```

## 1. 定位与差异化

| | learn-claude-code | learn-harness-engineering | **learn-self-evolving-skills** |
| --- | --- | --- | --- |
| 教什么 | 从零手搓 Agent 本体 | 给 Agent 搭工作环境（harness） | **让 Agent 的能力（Skill）随数据自动进化** |
| 学生写什么 | Agent 循环本身 | 工作区/索引/回路模板 | **进化链路：judge → evaluator → 测试集构造 → evolve → gate** |
| 环境 | 自己搭 | 基础应用给定 | 电商环境、数据包、CLI 骨架给定 |
| 教学法 | 逐版本递进 | 每项目 weak vs strong 对照 | **每课解法=下一课 starter；每课强制对照数字** |
| 可视化 | 无（纯终端） | Electron 应用充当活前端，对比靠手动 checklist | **课程自产 HTML 评测报告 = 活前端，对比数字系统自动产出** |

借用的经验：learn-harness-engineering 的链式 starter 与对照实验纪律；learn-claude-code 的「亲手实现核心机制」深度。不绑定、不致敬任何上游框架；SkillOpt、SkillForge、SkillCAT、DSPy、skill-up、skill-eval 作为各课拓展阅读与机制事实库。

## 2. 受众与前置要求

目标受众：

- 已写过至少一个简单 Agent（用过 function calling 或任一 Agent 框架）；
- 理解 MCP 的基本概念（工具即服务）；
- Python 熟练（Pydantic、类型标注、pytest 能读能写）。

## 3. 交付标准

**主产出是系统，Skill 是证据。**

结课验收（全部满足）：

1. 学生实现的全部模块通过课程测试套件（每课 starter 附测试）；
2. baseline、v0、每个候选版、final 全部产生**新的线上轨迹**（不能用缓存冒充）；
3. 每条轨迹包含：消息、工具参数与结果、StateDiff、判定结果、token、成本、Skill hash；
4. 候选版本经历过至少一次「接受」和一次「拒绝或回滚」；
5. 第 10 课自动循环在预算护栏内完成 ≥2 轮进化并输出进化曲线；
6. final 12 题报告完成，Agent 全程未接触 Gold、参考轨迹和留出答案；
7. 学生亲手构造的新题（≥5 道）通过「考卷先考自己」三步自查并入库。

学生简历句：

> 我亲手实现了一个带评测门控的 Skill 自进化系统（Python + Claude Code headless）：从真实客服轨迹生成退货 Skill，构建含真日志挖掘的测试集管线，经评测驱动的多轮自动进化与门控回滚，在全留出测试集上验证提升。

作品集导出物：`ses portfolio` 生成 Skill 各版本、版本谱系（含拒绝记录）、final 报告、进化曲线（L3 HTML 报告）、架构图和一页系统说明。

## 4. 课程大纲（6 部分 10 课）

结构约定：每课 = 概念讲义（困惑 → 方法 → 业界做法 → insight）+ 学生实现模块（starter/solution/tests）+ 一个对照数字 + 拓展阅读 + 预估成本。上一课的 solution 是下一课的 starter。

### P1 看见（课 1）

| 课 | 标题 | 学生动手 | 对照数字 | 知识点与拓展阅读 |
| --- | --- | --- | --- | --- |
| 1 | 你的第一个 Skill：先看见差别 | `doctor`（验 Key/模型/MCP/Claude Code）；**用课程写好的提示词驱动 Skill Creator，亲手生成自己的 demo Skill**；裸跑 1 个退货 case ⟷ 装 Skill 重跑，肉眼对比两段对话 | 同一 case 的 with/without 对话记录（定性） | Skill = 按需注入上下文的说明书；SKILL.md 解剖（description/流程/references）；触发机制。拓展：Anthropic Agent Skills 文档、STATE-Bench 设计 |

可靠性设计：学生生成的 Skill 质量有波动——仓库附一份参考 Skill 兜底；讲义顺势点出「Skill 质量会波动，『感觉变好了』可信吗？」引出 P2。

### P2 怎么评（课 2-4）

| 课 | 标题 | 学生动手 | 对照数字 | 知识点与拓展阅读 |
| --- | --- | --- | --- | --- |
| 2 | 终态不撒谎：状态与规则判分 | `trace.py`（stream-json → Trace 模型）；`judges/state.py`（订单库 snapshot/diff）；`judges/rule.py`（preview→confirm 顺序、`tool_called` 断言、failure 优先）；`expect` 零成本前置门 | develop 6 题 baseline 的 state pass 率 | Eval-Driven Development vs TDD；三层评估框架总览（触发层/结构层/验证层）；「说了 ≠ 做了」——判环境终态而非轻信 transcript。拓展：tau-bench reward 设计、Anthropic evals 指南 |
| 3 | LLM Judge 校准与 Agent-as-a-Judge | `judges/llm.py`（rubric 单调用、`not_evaluated` 出口）；`judges/agent.py`（**证据脚本先抽事实：StateDiff/工具时间线/金额对账 JSON → judge agent 读证据出 grading.json**）；两种 judge 与人工判的一致率对比实验 | 两种 judge 的人工一致率对比 | position/verbosity/self-preference 偏差与校准；**「脚本产出事实，LLM 基于事实判断」**（内部实践一致率 60%→85%）；何时用哪种（「看一眼」→LLM judge，「做点什么」→Agent judge）。拓展：Agent-as-a-Judge 论文、skill-up `agent_judge` |
| 4 | 模拟用户、批量运行与报告 | 对接给定 `simulator.py`；`evaluator.py`（`--resume` 多轮驱动）；`runner.py`（case/轮数/token/费用四重护栏、`--iteration` 重跑）；`report.py` 产出 **L1 HTML 报告** | baseline 完整报告（成功率/成本/耗时/方差） | 模拟用户必须被约束（intent 驱动、防泄漏、只说 want 不说 how，tau2 治理）；pass^k 而非 pass@1——方差本身是被测对象。拓展：skill-eval 模拟器机制、τ²-Bench |

### P3 考题哪来（课 5-6）

| 课 | 标题 | 学生动手 | 对照数字 | 知识点与拓展阅读 |
| --- | --- | --- | --- | --- |
| 5 | 从真日志到题目候选（S1-S3） | 清洗 ABCD 退货退款子集 1,070 段（去重/脱敏，**delexed 版可对答案**）；意图聚类（**flow/subflow 标注可自评**）；tau2 轨迹去重 + 按通过率难度分层采样 | 1,070 段 → N 个意图簇 → 分层采样清单 | 六阶段流水线（Scrub→Cluster→Stratify→Verify→Calibrate→Split）；语义去重难于字面去重；长尾高风险意图别当噪声丢；按难度分层而非全难题。拓展：Arena-Hard/BenchBuilder、WildBench |
| 6 | 从候选到黄金题（S4-S6） | `testset/variant_gen.py`（**变体生成器**：变异会员等级/窗口/促销组合，policy 引擎自动算 gold）；**考卷先考自己**三步（环境重放对账 → judge 试判故意对/错答卷 → 人工抽读）；合格题入 develop（**6 → 15+**） | 新题合格率 + 扩容后 develop 重跑 baseline | gold 由确定性脚本生成保证可验证；judge 校准先于考试；切分纪律（selection/final 锁死不动）。拓展：Benchmark Everything（自动造题路线，与日志提炼互补）、STATE-Bench 构造 |

教学叙事：真日志给「考什么」（意图分布、真实表达、边界情况），受控环境给「怎么判」（可执行、可重放、答案保证对）——工业界从日志到测试集的真实分工。

### P4 生成（课 7）

| 课 | 标题 | 学生动手 | 对照数字 | 知识点与拓展阅读 |
| --- | --- | --- | --- | --- |
| 7 | 从轨迹生成 v0 与首次定量对照 | creator workspace 隔离 + `static_gate`（工具白名单/无固定答案/长度）；驱动 Creator Adapter 从 9 条轨迹生成 v0；`trigger_eval.py`（20 条正负 prompt 算 P/R）；develop 上 v0 vs baseline 成对比较，产出 **L2 对照报告** | v0 触发 precision/recall + v0 vs baseline 配对表 | 三层评估框架落地（触发层=trigger_eval、结构层=static_gate、验证层=with/without）；description 设计；失败轨迹别混进种子（蒸馏风险）。拓展：skill-creator 方法论、SkillForge 从日志归纳 |

### P5 进化（课 8-9）

| 课 | 标题 | 学生动手 | 对照数字 | 知识点与拓展阅读 |
| --- | --- | --- | --- | --- |
| 8 | 失败分析与结构化补丁 | `evolution.py`：失败卡片（**六分类字段**：触发错误/模式错误/问题过载/术语暴露/时机不当/安全越界）→ 证据定位 → `add/update/delete` 小补丁 → v1 候选 | v1 补丁清单及每条的证据链 | 归因顺序：先怀疑考场和考卷，最后才是技能；最小补丁原则；**进化方法版图**（SkillOpt 六阶段/SkillForge 归因/SkillCAT 补丁验证/GEPA/TextGrad/ACE 分类表）；为什么选「小补丁+门控」——三篇论文共识最小交集 + ¥50 预算唯一可行解。拓展：SkillOpt `gradient/reflect`、SkillForge 四维归因 |
| 9 | 门控与版本治理 | `gate.py`（先触发门 → selection live 比较 → 回归/成本失控拒绝）；`registry.py`（谱系、promote/reject/rollback） | v0 vs v1 的 gate 决策记录 | 留出集门控保单调改进（SkillOpt 消融：无门控掉 2-4pp）；**真退化 vs 裁判漂移**要分开讲；模型更新导致 Skill 退化——回归测试的动机；TextGrad 无界重写的负增益是现成反面教材。拓展：SkillOpt `evaluation/gate`、SkillCAT 补丁级验证 |

### P6 串联（课 10）

| 课 | 标题 | 学生动手 | 对照数字 | 知识点与拓展阅读 |
| --- | --- | --- | --- | --- |
| 10 | 自动进化循环与 final | `auto_evolve.py`：`while 未达标且预算未超：rollout→reflect→patch→gate`，内置**冷却期/冻结阈值防震荡**；跑 final 12 题；`portfolio` 导出（**L3 进化报告**） | 进化曲线 + final 全留出报告 | 自动循环护栏（渐进式单方面修改、冷却期、改进冻结）；无限自改的风险；全架构回顾。拓展：SkillOpt 完整六阶段、skill-upper 对话式循环（对比：本课为结构化补丁+门控）、DSPy 优化器视角 |

刻度说明：

- 课 1 的对比是**定性肉眼**（给动机），课 2-4 学测量，课 7 起才有定量对照——「先看见、再测量、后优化」；
- 不实操 SkillOpt 的 aggregate/编辑预算/负反馈缓存，拓展阅读指出「生产系统还差什么」；被拒补丁缓存、成败对比找分歧点列第二版候选。

## 5. 课程要教的核心 insight（讲义素材，调研已验证）

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

- **讲义**：纯 markdown + mermaid（GitHub 原生渲染），每课末尾贴「你应该看到的报告示例」；
- **活前端 = 课程自产 HTML 评测报告**（对标项目的对照靠手动观察，我们的对照系统自动产出，这是天然差异化）；
- VitePress 书站缓行（对标项目都是内容火了才补网站）。

HTML 报告三层（单文件自包含：内嵌 JSON+图表，双击可开、可分享、<2MB、不含密钥；指标旁有 ⓘ 教学注释）：

| 层 | 交付课 | 内容 |
| --- | --- | --- |
| L1 单次运行 | 课 4 | 运行摘要（pass 率/成本/token/方差）+ 逐 case 卡片：判定结果、失败证据摘录、工具调用时间线、StateDiff 表、可折叠 transcript |
| L2 对照 | 课 7 | with/without 成对柱状图 + 配对表（哪题翻转/回退）+ 分数分布 |
| L3 进化 | 课 10 | 版本谱系图（含被拒分支）、跨版本进化曲线（分数+成本双轴）、gate 决策记录表、final 页 |

## 7. 给定脚手架 vs 学生自写边界

| 给定（脚手架，含测试） | 学生自写（课程主体） |
| --- | --- |
| 电商数据包（切分、manifest、SHA256） | `trace.py`（课 2） |
| `shop/`：订单状态、政策计算（**变体生成的 gold oracle**）、11 个工具、MCP server | `judges/state.py`、`judges/rule.py`（课 2） |
| `runtime.py`：每 case 独立 cwd 与 Claude Code 配置目录隔离 | `judges/llm.py`、`judges/agent.py` + 证据脚本（课 3） |
| `simulator.py`：用户模拟器（持私有 intent，不碰写工具，防泄漏规则） | `evaluator.py`、`runner.py`、`report.py`（L1-L3）（课 4/7/10） |
| ABCD/tau2 原始切片 + embedding/聚类库封装 | `testset/clean.py`、`testset/sample.py`（课 5） |
| Creator Adapter（安全工具白名单，固定上游 skill-creator） | `testset/variant_gen.py`、`testset/calibrate.py`（课 6） |
| CLI 骨架 `ses`（命令解析，不含业务逻辑） | `static_gate`、`trigger_eval.py`（课 7） |
| demo Skill 的 Creator 提示词 + 参考 Skill 兜底（课 1） | `evolution.py`、`gate.py`、`registry.py`（课 8-9）；`auto_evolve.py`（课 10） |

原则：**凡属进化链路与测试集管线的判断逻辑一律学生写；凡属环境仿真与安全隔离一律给定**。给定件学生要会读（讲义指定阅读段落）。

## 8. 技术栈与执行引擎

- **执行引擎**：Claude Code headless（`-p` + stream-json），版本锁定在 `models.lock.yaml`。
  - Skill 用原生 SKILL.md 自动发现；触发评测检验真实产品行为。
  - **多轮会话**：首轮 `claude --session-id <id> -p ...`，后续轮 `claude --resume <id> -p ...`（skill-up `SessionResumer` 同一模式）。
  - 命令基线：`--settings '{"disableAllHooks":true}' --permission-mode=<mode>`；每 case 独立配置目录：只装被测 Skill，关闭个人 Skill/memory/extensions，不挂载 Gold。
  - **权限差异**：skill-up 在受信沙箱用 `bypassPermissions`；课程不用 yolo/bypass 代替隔离，用受限权限模式 + MCP 工具白名单。
- **Skill 安装机制**：把 `SKILL.md` + references 按 include/exclude 拷入 `.claude/skills/`，`evals/` 永不安装（课 1/7 讲授基点）。
- **Agent-as-a-Judge 落地形态**（课 3）：确定性脚本先产证据文件（StateDiff、工具时间线、金额对账）→ judge agent（headless + 只读工具）读证据出 grading.json（逐断言 PASS/FAIL + evidence）。
- **模型接入**：国内 Anthropic 兼容端点，默认**硅基流动**单 Key：
  - 主 Agent / Creator：DeepSeek 系（Anthropic 兼容端点接 Claude Code）；
  - User Simulator / LLM Judge / Agent Judge：Qwen 系便宜档（OpenAI 兼容端点，Python 直调或 headless）；
  - 兜底：`claude-code-router`；必须用标准按量 Key，订阅制 Coding Plan Key 不允许批量评测。
- **语言与依赖**：Python 3.11+，Pydantic v2，pytest；不引入重型 Agent 框架。
- **仓库结构**：

```text
learn-self-evolving-skills/
├── README.md                      # 中文主线 + 英文摘要
├── pyproject.toml
├── ses.yaml                       # 全局配置
├── models.lock.yaml               # 模型/CLI 版本锁定
├── course/
│   ├── ch01-see-the-difference/   # 每课：讲义.md + starter/ + solution/ + tests/
│   ├── ch02-state-and-rules/
│   ├── ch03-llm-and-agent-judge/
│   ├── ch04-simulator-and-runner/
│   ├── ch05-mine-real-logs/
│   ├── ch06-golden-cases/
│   ├── ch07-create-v0/
│   ├── ch08-failure-to-patch/
│   ├── ch09-gate-and-registry/
│   └── ch10-auto-evolve/
├── data/
│   ├── upstream/
│   │   ├── state-bench/           # 固定 commit + MIT License
│   │   ├── abcd/                  # product_defect 等子集切片 + MIT License
│   │   └── tau2-logs/             # retail 轨迹切片（只读挖掘用）+ MIT License
│   └── skill-packs/resolve-product-returns/
├── skills/resolve-product-returns/   # v0、候选、accepted（学生运行时生成）
├── src/ses/
│   ├── cli.py  config.py  credentials.py  dataset.py
│   ├── trace.py  evaluator.py  runner.py  report.py
│   ├── judges/{state,rule,llm,agent}.py
│   ├── testset/{clean,sample,variant_gen,calibrate}.py
│   ├── skill_creator.py  trigger_eval.py
│   ├── evolution.py  gate.py  registry.py  auto_evolve.py
│   ├── engines/claude_code.py
│   ├── shop/{state,policy,tools,mcp_server}.py
│   ├── runtime.py  simulator.py
├── tests/
├── scripts/prepare_data.py
└── runs/                          # 不提交 Git
```

CLI 命令面：

```bash
ses doctor
ses skill create --guided          # 课1：用课程提示词驱动 Creator 生成 demo Skill
ses eval run --split develop --without-skill
ses eval run --split develop --skill v0 --force-skill
ses inspect <run-id> <case-id>
ses testset mine                   # 课5：清洗/聚类/采样
ses testset generate               # 课6：变体生成
ses testset calibrate              # 课6：考卷先考自己
ses skill create --from-traces --out v0   # 课7
ses trigger-eval --skill v0
ses evolve <run-id> --from v0 --out v1
ses gate --old v0 --candidate v1
ses auto-evolve --budget 15 --max-rounds 3
ses final --skill accepted
ses portfolio <run-id>
```

## 9. 数据设计（双源方案）

### 9.1 三个数据源

| 源 | 内容 | License | 角色 |
| --- | --- | --- | --- |
| **STATE-Bench** `customer_support`（固定 commit `5644b183`） | 33 个退货退款任务 + 21 条完整工具轨迹 + 独立订单环境 | MIT | **执行主线**：所有 live 评测都跑在这个环境 |
| **ABCD**（`product_defect` 等子集） | 1,070 段真人扮演退货退款对话（口语、错字、寒暄），自带原文→脱敏双版本与意图标注 | MIT | **挖题原料**（课 5 S1-S3）：清洗可对答案、聚类可自评 |
| **tau2-bench** retail 历史轨迹 | 1,824 条 Agent 运行日志（114 任务×16 次，含工具调用+reward） | MIT | **辅料**（课 5）：同任务多次运行教去重，按通过率教难度分层；只读挖掘，不重放执行 |

数据诚实性：全部为 benchmark/角色扮演数据，课程话术是「生产式业务流程/真人对话语料」，不是「真实生产日志」；报告标注「基于 STATE 数据的课程评测」。

### 9.2 切分（selection/final 锁死，develop 可扩容）

| 数据组 | 数量 | 用途 | Creator/Updater 可见 | Agent 可见 |
| --- | ---: | --- | --- | --- |
| `creator` | 9 | 生成 v0（已审核成功轨迹） | 全部 | 不运行 |
| `develop` | 6 → **15+**（课 6 学生扩容） | 反复运行与失败分析 | 失败轨迹 + Judge 证据 | 当前消息/Skill/工具结果 |
| `selection` | 6（**锁死**） | 门控比较 | 只见汇总分 | 同上 |
| `final` | 12（**锁死**） | 最终验收，不参与修改 | 不可见 | 同上 |
| `trigger-eval` | 20 | 触发评测（10 正 10 负） | — | — |

扩容规则：新题只能进 develop；每道新题必须通过「考卷先考自己」三步（环境重放对账 → judge 试判故意对/错答卷 → 人工抽读）；gold 一律由 `shop/policy.py` 计算，不允许人工手填。

程序性检查（脚手架内置，测试覆盖）：切分互斥；Creator 只读 `creator`；Agent workspace 无 Gold/任务要求/参考轨迹；每 case 全新订单环境；Skill 内容不得含 case/顾客/订单 ID。

### 9.3 预算拆解（目标 ≤¥50，每课讲义附实测校准表）

| 开销项 | 量级估算 |
| --- | --- |
| 课 1（demo Skill 生成 + 2 次对比跑） | ~¥3 |
| 课 2-4（baseline 6 题 + judge 一致率实验 + 模拟器批量） | ~¥8 |
| 课 5-6（聚类 embedding + 新题校准试判 + develop 重跑） | ~¥8 |
| 课 7（v0 生成 + 触发 20 prompt + with/without 成对） | ~¥8 |
| 课 8-9（补丁生成 + selection 门控） | ~¥8 |
| 课 10（自动循环 2-3 轮 + final 12 题） | ~¥12 |
| 返工余量 | ~¥8 |

护栏：每次运行前显示预算预估；`runner` 内置 case/轮数/token/费用四重上限；超限中断且保留部分轨迹。仓库附官方参考 run（只读），供无 Key 者阅读对照。

## 10. 课程规则（诚实性）

- 代码与数据全开源，隔离只防意外泄漏——**荣誉守则**：翻答案只欺骗自己的作品集；
- **final 一次性原则**：final 运行后再改 Skill 即视为新实验，报告须重新声明；
- `learn` 模式（每题 1 次）只能叫「教学结果」；写进简历的数字必须来自 `portfolio` 模式（每题 2-3 次，报均值与波动）；
- final 只有 12 题，讲义明说：「这是教学规模，方法论是工业级的」，报告必须写波动区间。

## 11. 首发前必须完成的验证清单

| # | 事项 | 产出 |
| --- | --- | --- |
| 1 | Claude Code headless 在硅基流动端点实测（stream-json 完整性、Skill 自动发现、配置隔离） | doctor 检查项 + models.lock |
| 2 | DeepSeek 在 Claude Code 下 tool calling 稳定性实测（30 case 抽样） | 失败率数据 + 备选模型预案 |
| 3 | 成本实测校准（每课跑 3 case 记录 token/费用） | §9.3 替换为实测值 |
| 4 | 课 1 demo Skill 提示词打磨：3 个不同学生水平模拟生成，对比效果都需肉眼可见 | Creator 提示词 + 参考 Skill |
| 5 | 9 条 creator 轨迹三重复核（State Judge + LLM Judge + 人工） | 审核记录入 manifest |
| 6 | Agent-judge 一致率实验：单调用 LLM judge vs 证据式 agent judge vs 人工，复现「60→85」课程版数据 | 课 3 讲义核心数据 |
| 7 | 变体生成器 oracle 一致性验证：生成题标准操作重放，终态与 policy 计算 100% 一致 | 课 6 脚手架测试 |
| 8 | ABCD/tau2 切片脚本 + license 文件随仓 | data/upstream 数据包 |
| 9 | trigger-eval 20 条 prompt 编写与交叉校验 | trigger-eval 数据包 |
| 10 | portfolio 模式方差实测（同配置 3 次重复） | gate 阈值依据写进 `ses.yaml` |
| 11 | L1/L2/L3 报告模板设计与单文件体积验证（<2MB） | report 模板 |
| 12 | 三篇论文 + skill-up/skill-eval 的每课映射精读笔记（读哪段、回答什么问题） | 各课「拓展阅读」小节 |

## 12. 运营默认值

- 正文中文主线，README 含英文摘要；
- License：代码 MIT，数据继承上游 MIT 并保留原始 LICENSE、commit、ID 与 SHA256；
- 首发标准：10 课全部含 starter/solution/tests，§11 清单十二项全部完成；
- CLI 名 `ses`（可在首发前调整）。

## 13. 与 skill-up 的基础机制对齐表

依据：对 skill-up 仓库（`internal/` + `docs/`）逐包检查（2026-08-15）。skill-up 是课程的「机制事实库」——凡它有而课程没有的基础件，按「学生亲手实现」或「列入 roadmap」处置。

### 13.1 已吸收进课程

| 机制 | skill-up 形态 | 课程处置 |
| --- | --- | --- |
| Headless 执行 | `claude -p` + stream-json，`--session-id`/`--resume` | §8 命令基线一致；`--resume` 驱动模拟器多轮（课 4） |
| Skill 安装 | 拷入 `.claude/skills/`，include/exclude，`evals/` 永不安装 | 课 1/7「Skill 注入」讲授基点 |
| 双层评分 | `expect`（零成本门）+ `judge` | 课 2：expect 门 + 判分漏斗 |
| rule_based 断言 | `output_contains/matches`、`tool_called`（args 部分匹配）、failure 优先 | 课 2 |
| agent_judge | judge agent 读证据与产物出分 | **课 3 正文**（证据脚本 + headless judge） |
| benchmark 模式 | `benchmark.enabled` 双跑 with/without | 课 7 对照实验参照 |
| 多轮会话 | `SessionResumer.RunTurn`（qwen 未支持则回退 batch） | 课 4；引擎选型影响机制的活教材 |
| 稳定性采样 | `--iteration N` | 课 4 + portfolio 模式 |
| 运行时隔离 | none / opensandbox / docker 三档 | 课程用独立配置目录；docker 档 roadmap |
| MCP mock | stdio mock + `tool_responses` 模板 | 机制讲授；mock 能力 roadmap |
| 自动进化 | `skill-upper` 对话式闭环 | 课 10 对比对象（本课为结构化补丁+门控） |
| CI 契约 | exit 0/1、JUnit、OTLP | 课 4 报告加分项；OTLP roadmap |

### 13.2 课程有意强于 skill-up 的点（差异化依据）

| 维度 | skill-up | 本课程 |
| --- | --- | --- |
| 预算护栏 | 无（仅 timeout/max_turns） | 四重上限 + ≤¥50 承诺 |
| 判分依据 | 文本断言为主 | State Judge + StateDiff 终态判定 |
| 用户模拟 | 脚本化 `input.turns` | intent 驱动模拟器（防泄漏） |
| 测试集管线 | 不提供 | 真日志挖掘 + 变体生成 + 考卷自查 + 四组切分 |
| 课程纪律 | — | 每课对照数字、final 一次性、portfolio 才算数 |

### 13.3 首版有意不做

- `input.turns` 的 `post_condition`/`capture` 模板机制（模拟器已覆盖，roadmap）；
- `script` judge 独立类型（证据脚本已体现其思想，拓展习题）；
- 多租户、队列、Web 后台、多引擎矩阵。
