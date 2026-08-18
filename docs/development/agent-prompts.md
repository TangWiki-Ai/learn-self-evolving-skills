# 多 Agent 启动提示词

## 分发顺序

1. 现在并发发送 Prompt A 和 Prompt B。
2. Prompt A 合并到 `main` 后，从新的 main 并发发送 Prompt C、D、E。
3. C、D、E 都完成 handoff 后发送 Prompt F，由一个 Agent 集成并关闭 Issue #2。
4. Issue #2 合并后，再按依赖图启动 #3、#4、#5。不要提前启动 #7-#12。

每个 Agent 必须使用独立 worktree 或平台提供的隔离 workspace。不要让多个 Agent 在同一个工作目录里切 branch。

## Prompt A - Bootstrap 与最小 Contracts

```text
你是 learn-self-evolving-skills 的 Bootstrap & Contract Owner。

仓库：<repo-root>
目标 branch：agent/bootstrap-contracts

先阅读根 AGENTS.md，并按其中顺序阅读 alignment、系统总览、跨模块契约、并行实施文档和 GitHub Issue #2。检查当前仓库后直接实施，不要就常规细节反复提问。平台已提供隔离 worktree 时直接使用；否则从最新 origin/main 创建独立 worktree 和 branch。

目标：建立后续三个领域 Agent 可以共同依赖的最小 Python 工程骨架和 Issue #2 contracts。只做骨架与跨模块接口，不实现 Engine、Shop 或 Judges 的业务逻辑。

你拥有：
- pyproject.toml、uv.lock、Ruff/mypy/pytest 配置
- src/ses/__init__.py
- src/ses/contracts/**
- src/ses/cli/app.py 及只用于证明命令注册的测试
- tests/contracts/**

必须交付：
1. Python 3.11+、PEP 621、src layout、uv；运行依赖只加入 Pydantic v2，开发依赖加入 pytest、mypy、Ruff。
2. console script `ses`，使用标准库 argparse。此阶段 `ses --help` 可运行；CLI 不承载业务逻辑。
3. 只为 Issue #2 冻结最小 contracts：base/version/IDs/artifact reference、EngineRequest/EngineEvent/Usage、CaseDefinition、Money、ShopSnapshot/StateDiff/ToolResult、Trace/EvidenceRef/AssertionResult/CaseGrade。遵守 docs/specs/10-cross-module-contracts.md。
4. Contract tests 覆盖 Pydantic frozen/extra forbid、JSON round-trip、enum、UTC、金额、相对路径、canonical hash 和凭据字段拒绝。
5. 在 contracts/README.md 记录 producer、consumer 和允许的变更流程，不复制 spec 全文。

硬边界：
- 不实现 Foundation、Claude adapter、Shop、MCP、Trace parser 或 Judges。
- 不改 Phase 0 行为，不调用付费模型，不下载完整数据。
- 不为 #3 以后预定义大量字段；发现未来需求只写 handoff 风险。

完成后运行 AGENTS.md 中全部离线检查，提交 Conventional Commit。不要 merge/push main，不关闭 Issue。按 parallel-implementation.md 的 handoff 模板回复，特别列出每个 contract 的字段和任何仍需决策的接口。
```

## Prompt B - 固定数据与挖题流水线

```text
你是 learn-self-evolving-skills 的 Data Mining Owner，负责 GitHub Issue #6。

仓库：<repo-root>
目标 branch：agent/data-mining

先阅读根 AGENTS.md、产品 alignment、cross-module contracts、testset spec、并行实施文档、PRD 数据章节和 Issue #6。平台已隔离 workspace 时直接使用；否则从最新 origin/main 创建独立 worktree。你可以与 bootstrap Agent 同时工作，但 bootstrap 合并前不能编辑 pyproject.toml、uv.lock、src/ses/contracts/** 或共享 CLI。

你拥有：
- src/ses/testset/**
- scripts/prepare_data.py
- data/upstream/** 中的小型固定切片、manifest、LICENSE/source notes
- tests/testset/**

目标：交付可复现的数据获取、校验、清洗、去重、标签对照和难度分层，不把 ABCD/tau2 当成可执行 shop case。

必须使用这些已验证事实：
- STATE-Bench commit 5644b1838d96bc4483da29642d058ecaa6f80f7f；按 JSON `task_type == return_item` 得到 33 tasks，其中 21 个有 train trajectory。
- ABCD commit 6b8700ce67c6b37b062dd7a60abc76d7ef832a97；10,042 conversations，`scenario.flow == product_defect` 得到 1,070；保留 original/delexed/subflow。
- tau2 commit c3398666e6559e3a063da3fc04b5acf7f941464e；retail 114 tasks，4 个 4-trial 结果文件，共 1,824 trajectories；只用于去重和难度信号。

必须交付：
1. 固定来源、license、SHA256、切片条件和转换版本的机器可读 manifest。
2. 默认 CI 只使用小 fixture；完整下载显式触发、可重试、校验通过后再原子落盘。不要提交完整上游仓库或大文件。
3. Scrub 保留 original/delexed 对应，生成稳定 source ID，处理空值、编码和重复，不改写 intent。
4. tau2 先按 task 聚合 16 次运行，再计算难度；不能把 1,824 条轨迹当独立题。
5. Cluster/Stratify 使用可注入 adapter。单测用 deterministic fake；生产 adapter 若需要 scikit-learn，等 bootstrap 合并并 rebase 后再提交最小 optional dependency 变更，不引入远程 embedding 服务。
6. 输出可审计 candidate list、标签对照指标、难度桶和 funnel counts；不写 creator/selection/final，只生成候选。
7. 测试覆盖 exact filter、稳定 ID、去重、标签比较、难度聚合、长尾保留、manifest 漂移和网络禁用。

公开文字只能称 benchmark 或角色扮演数据。不要实现 Shop、Judge、variant/gold/calibration，也不要调用付费模型。

完成后同步最新 main，运行全部离线检查并提交 branch。不要 merge main，不关闭 Issue #6。按 handoff 模板回复，明确哪些 acceptance criteria 已完成、是否需要 optional dependency，以及未下载/未运行的内容。
```

## Prompt C - Foundation Runtime 与 Claude Engine

```text
你是 learn-self-evolving-skills 的 Foundation & Engine Owner。只有 bootstrap/contracts 已合并到 main 后才开始。

仓库：<repo-root>
目标 branch：agent/foundation-engine

从最新 origin/main 创建隔离 worktree，阅读 AGENTS.md、alignment、system overview、cross-module contracts、foundation spec、parallel implementation 和 Issue #2。直接实施，不重复讨论已确认的 Provider 选择。

你拥有：
- src/ses/foundation/**
- src/ses/engines/**
- src/ses/cli/doctor.py
- tests/foundation/**、tests/engines/**
- scripts/phase0_check.py、scripts/phase0_mcp_server.py 的兼容重构

目标：实现小而深的 Foundation Runtime 和两个真实 adapter：ClaudeCodeEngine 与 FakeEngine。所有模型角色首版都走 Claude Code headless + SiliconFlow；不要增加 Python OpenAI client、router 或 fallback 框架。

必须交付：
1. 严格项目配置、models lock、环境凭据读取和全量脱敏；Key 不进入 argv、日志、artifact 或 MCP server env。
2. 每 case 临时 workspace 和 CLAUDE_CONFIG_DIR，allowlist 安装内容，隔离全局 settings/skills/memory。
3. ClaudeCodeEngine 使用参数数组运行 `claude --bare -p --output-format stream-json`，支持新 session/resume、timeout/cancel/process cleanup，并只输出 canonical EngineEvent。
4. FakeEngine 从声明式 fixtures 重放文本、工具调用/结果、usage、畸形事件、非零退出和 timeout。
5. 将 Phase 0 核心逻辑迁入 package，保留现有脚本作为薄兼容入口；`ses doctor` 使用同一实现。
6. 默认测试完全离线。保留显式 live marker，但不要自行使用真实 Key。

只消费已冻结 contracts。需要改字段时不要编辑 contracts，按 handoff 提交 proposal。不要实现 Shop、Judges、Evaluator 或报告。

完成后运行全部离线检查，提交 branch，不 merge main、不关闭 Issue #2。Handoff 必须给出 Engine interface、事件 fixture 列表、子进程安全证明和未运行的 live 检查。
```

## Prompt D - Shop Environment 与 MCP

```text
你是 learn-self-evolving-skills 的 Shop & MCP Owner。只有 bootstrap/contracts 已合并到 main 后才开始。

仓库：<repo-root>
目标 branch：agent/shop-environment

从最新 origin/main 创建隔离 worktree，阅读 AGENTS.md、alignment、cross-module contracts、shop spec、parallel implementation、PRD 数据设计和 Issue #2。

你拥有：
- src/ses/shop/**
- tests/shop/**
- tests/fixtures/shop/**

目标：用固定 STATE-Bench return_item case 跑通确定性订单环境、政策 oracle、必要工具、MCP、snapshot 和 StateDiff。Issue #2 先证明单 case 纵向链，不扩建完整电商平台。

必须交付：
1. 先核对固定 commit 中 customer_support 的实际工具与 schema，提交简短映射；不要凭 PRD 猜 11 个工具名称。
2. 选择并记录一个能代表退货状态变更的固定 case，保留 upstream task ID、commit 和转换版本。
3. CaseEnvironment 提供 reset/snapshot/execute/close；每次从 fixture clone，允许并行且互不污染。
4. 政策计算纯确定性；业务金额使用 Money/minor units；写工具验证权限和政策并原子变更，失败无部分写入。
5. 实现该 case 所需的最小工具集和 stdio MCP server。不要伪装未实现工具；剩余工具明确留给后续 ticket。
6. Snapshot 稳定排序，StateDiff 只含业务变化和人类摘要，摘要不参与 Judge。
7. 测试覆盖 policy 边界、重复调用、回滚、MCP list/call、并行隔离、稳定 diff 和权限拒绝；默认不访问网络。

只消费 contracts，不编辑 Engine、Evaluation、CLI app 或 shared contracts。需要接口变化时提交 contract proposal。

完成后运行全部离线检查并提交 branch。不要 merge main、不关闭 Issue #2。Handoff 列出选定 case、已实现工具、未实现工具、政策不变量和测试结果。
```

## Prompt E - Trace 与确定性 Judges

```text
你是 learn-self-evolving-skills 的 Evaluation Core Owner。只有 bootstrap/contracts 已合并到 main 后才开始。

仓库：<repo-root>
目标 branch：agent/evaluation-core

从最新 origin/main 创建隔离 worktree，阅读 AGENTS.md、alignment、cross-module contracts、evaluation spec、parallel implementation 和 Issue #2。

你拥有：
- src/ses/evaluation/**
- tests/evaluation/**
- tests/fixtures/stream_json/**

目标：实现 Issue #2 所需的 Trace parser、零成本 expect、State Judge、Rule Judge 和 failure-first 聚合。LLM Judge 与 Agent Judge 属于 Issue #5，本 branch 不实现。

必须交付：
1. 从 canonical EngineEvent 构造不可变 Trace，保留 sequence、稳定 event ID、session、messages、tool calls/results、usage 和 exit status。
2. 使用脱敏后的真实形状 stream-json fixtures 覆盖 text chunks、tool use/result、unknown event、malformed critical event、truncate 和 non-zero exit。
3. expect 在模型运行前验证 case、fixture、必要工具、预算和环境前置，失败不产生 Engine 调用。
4. State Judge 消费 expected snapshot/StateDiff，逐断言给出实际、预期和 EvidenceRef。
5. Rule Judge 支持 tool called、次数、参数约束、顺序和 forbidden call；实现 failure-first。
6. 聚合严格区分 pass、fail、not_evaluated 和 error；基础设施错误不能算 Agent fail。
7. 测试覆盖 parser、evidence references、金额、顺序、failure-first 和 producer-consumer contract。

只消费 Engine 和 Shop contracts，不依赖其实现。不要复制 StateDiff/Trace schema，不编辑 Shop、Engine、CLI app 或 contracts。缺字段时提交 contract proposal。

完成后运行全部离线检查并提交 branch。不要 merge main、不关闭 Issue #2。Handoff 列出 fixture shapes、支持的断言、聚合真值表和测试结果。
```

## Prompt F - Issue #2 集成

```text
你是 learn-self-evolving-skills 的 Issue #2 Integration Owner。Bootstrap 已在 main，Foundation、Shop、Evaluation 三个 lane 已提供 branch/commit handoff 后才开始。

仓库：<repo-root>
目标 branch：agent/integrate-issue-2

创建隔离 integration worktree。先阅读 AGENTS.md、parallel implementation、Issue #2 和三个 handoff。按 bootstrap -> foundation -> shop -> evaluation 顺序整合提交；保留各模块已通过行为，不做无关重构。

你拥有集成改动：
- src/ses/evaluator/**
- src/ses/reporting/** 中 Issue #2 最小 L1 view
- src/ses/cli/app.py 和 Issue #2 command wiring
- tests/integration/** 及必要的集成 fixture
- 经 proposal 审核后的最小 contract 修订

必须交付：
1. 一个 `ses` 命令从固定 STATE-Bench case 创建独立 workspace，启动 fake engine/MCP 形状，生成 Trace、before/after snapshot、StateDiff、State/Rule grades 和稳定 run ID。
2. `ses inspect <run-id> <case-id>` 或等价入口展示最小 L1 结果：消息、工具输入输出、StateDiff、断言、usage/cost 字段和 Skill hash 字段。
3. CLI 级默认测试用 FakeEngine 跑完整路径，不访问网络、不读取 Key，并验证 fresh workspace 和不可变 artifact。
4. 错误路径区分 expect fail、Agent fail、Judge error、infrastructure error 和 budget stop。
5. 更新 README 的最短命令和 Issue #2 状态；不提前实现 batch、LLM Judge 或完整 HTML。
6. 运行所有 Ruff、mypy、pytest、Phase 0 offline smoke 和凭据扫描。真实 live 单 case 只有得到明确授权才运行。

验收必须对应 Issue #2 的每条 acceptance criterion。通过后提交 integration branch，提供完整 handoff；由仓库 owner 决定 merge 和关闭 Issue，不自行改其他 Issue 依赖。
```
