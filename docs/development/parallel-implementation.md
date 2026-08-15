# 多 Agent 并行实施

## 基本原则

Spec 定义模块长期行为，GitHub Issue 定义一个可验收纵向切片。并行 Agent 按 Issue 和文件所有权工作，不按“一人一份完整 spec”工作。后者会绕过依赖图，并让多个 Agent 同时创建共享基础设施。

每个 wave 只允许一个 Agent 修改 shared contracts、根配置、CLI 聚合入口和 lockfile。领域 Agent 通过已冻结 contract 开发，不能复制类型规避等待。

## 依赖波次

```text
Wave 0  [done]  #1 Phase 0 smoke

Wave 1A [serial] bootstrap + minimum contracts
Wave 1B [parallel after 1A]
         foundation/engine lane --\
         shop/MCP lane -----------> #2 integration
         trace/judges lane -------/
         data-mining lane ----------> #6

Wave 2  [after #2] #3 Skill demo, #4 batch/L1, #5 judge calibration
         #6 may continue in parallel

Wave 3  [after #2 + #5 + #6] #7 verified develop cases
Wave 4  [after #3 + #4 + #7] #8 Skill v0 + L2
Wave 5  #9 patch -> #10 gate/registry -> #11 automation -> #12 release
```

当前不要启动 #7-#12 的实现 Agent。它们可以读 spec，但不能先写占位框架或猜测上游接口。

## Wave 1 文件所有权

| Lane | Owns | Must not edit |
| --- | --- | --- |
| Bootstrap/contracts | `pyproject.toml`, `uv.lock`, root tool config, `src/ses/contracts/**`, package skeleton, `src/ses/cli/app.py` | 领域实现、课程内容 |
| Foundation/engine | `src/ses/foundation/**`, `src/ses/engines/**`, `src/ses/cli/doctor.py`, `tests/foundation/**`, `tests/engines/**`, Phase 0 scripts | Shop、Evaluation、shared contracts |
| Shop/MCP | `src/ses/shop/**`, `tests/shop/**`, `tests/fixtures/shop/**` | Engine、Evaluation、CLI app、shared contracts |
| Trace/judges | `src/ses/evaluation/**`, `tests/evaluation/**`, `tests/fixtures/stream_json/**` | Shop 实现、Engine 实现、CLI app、shared contracts |
| Data mining | `src/ses/testset/**`, `scripts/prepare_data.py`, `data/upstream/**`, `tests/testset/**` | Shop fixtures、CLI app、shared contracts；bootstrap 合并前不改依赖文件 |
| #2 integrator | `src/ses/evaluator/**`, `src/ses/reporting/**`, `src/ses/cli/app.py`, integration fixtures/tests | 大规模重写已通过的领域模块 |

`src/ses/contracts/**` 由 contract owner 串行修改。领域 Agent 需要变更时，在 handoff 中提供 proposal：字段、类型、不变量、生产者、消费者和迁移影响。

## Package Layout

```text
src/ses/
  cli/          # argument parsing and presentation
  contracts/    # versioned cross-module records
  foundation/   # config, credentials, datasets, workspaces
  engines/      # Engine adapters and fake engine
  shop/         # deterministic policy, state, tools, MCP
  evaluation/   # Trace, evidence, expect and Judges
  evaluator/    # single-case orchestration
  runner/       # batch, resume and budgets
  reporting/    # L1/L2/L3 renderers
  testset/      # acquisition, scrub, cluster, stratify, verify
  skills/       # install, create, static and trigger gates
  evolution/    # failures, patches, gate and registry
  automation/   # bounded loop and portfolio
```

模块只能通过 contracts 或对方公开接口协作。CLI 不成为业务逻辑中转站。

## Git Protocol

1. 从最新 `origin/main` 创建 `agent/<lane>` branch 和独立 worktree。
2. 开工前记录 base commit 和负责路径。
3. 先写或更新本 lane 的外部行为测试，再实现最小 ticket slice。
4. 遇到 contract 缺口时继续处理不依赖部分，并提交 proposal；不在本 lane 发明替代模型。
5. 提交前同步 main，运行仓库完整检查，提交 Conventional Commit。
6. Handoff 给 integrator，不直接 merge main。

Integrator 按 `bootstrap -> foundation/shop/evaluation -> integration` 顺序合并。Data mining 使用独立 Issue，可在不破坏 contracts 的前提下单独合并。

## Handoff Template

```text
Branch / commit:
Base commit:
Issue and acceptance criteria covered:
Owned files changed:
Contracts consumed:
Contract proposals:
Commands run and results:
Live/network checks not run:
Known risks or follow-up:
```

只有 integrator 跑通对应 Issue 的 CLI 级验收、默认离线测试和凭据扫描后，才关闭 Issue。
