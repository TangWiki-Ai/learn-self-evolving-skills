# Shopping Assistant 独立 Capstone

这个毕业项目不进入现有十课编号。你会把前十课的方法迁移到一个
`ShopSimulator-inspired fixed workflow`，亲手完成完整的学习闭环：

```text
create → Static/Trigger → fresh pair → failure evidence → evolve
       → protected Gate → Registry → auto-evolve → one-time final
       → L3/portfolio → accepted-only package/install
```

当前 Phase 0 结论是 `no_go`。因此 fixed 路线使用课程原创 fixture，不联网、不读 Key、
实际新增付费为 0。它不代表真实 ShopSimulator 用户收益。live 路线保持关闭，详情见
[`LIVE_SETUP.md`](LIVE_SETUP.md)。

## 学习目标

完成后，你能解释并证明：

1. learner v0 来自脱敏的成功轨迹投影，不来自 packaged reference Skill；
2. Static、10/10 Trigger 和 fresh paired evaluation 各自回答什么问题；
3. raw reward、shopping metric 和课程 `CaseGrade` 为什么必须分开；
4. 哪些失败属于 Skill，哪些失败必须停止 Updater；
5. safety hard fail、critical regression、strict regression 和 tie 为什么阻止提升；
6. Registry 为什么保留接受与拒绝分支，final 为什么只能运行一次；
7. 为什么只有 current accepted 且 final 合格的 Skill 能 package 和 install。

你的完成状态必须达到 `learning_completion=workflow_complete`，fixed 测量级别必须是
`synthetic_offline`。一份 reference_fallback 或作者生成的静态报告都不能替代你的 receipts。

## 课程提供

课程已经提供稳定、低教学价值的基础设施：

- fixed/live profile schema、10 个 source groups 和 40 个 episode slots；
- `ShopSimulatorPort` 的 HTTP/in-memory Adapter、MCP gateway 和 one-use TurnLease；
- workspace、episode 生命周期、raw payload 归一化、artifact hash 和安全 evidence；
- Creator、Runner、fresh pair、八阶段 Gate、Registry、automation、final 与 release seam；
- 课程原创 fixed fixture、故障注入、CLI 目标接口和 clean-room validator。

这些组件不会替你作出领域判断。

## 你要实现

你按五个 milestone 工作。每个 starter 都保留一个明确缺口，solution 只展示当前 milestone
如何绑定生产 seam。

| Milestone | 你实现的判断 | 必须看见的产物 |
| --- | --- | --- |
| Create | seed projection、shopping Static policy、Trigger suite | learner v0、Static receipt、10/10 Trigger receipt |
| Eval | grade projection、fresh pair 可比性、四类 strata | pair receipt、Trace、raw/metric/grade、L2 |
| Evolve | shopping subcode、Skill 根因、最多 3 个 evidence-linked 操作 | Failure Evidence、Failure Card、Patch、candidate |
| Gate | shopping aggregate、safety/critical guardrail、正确分支 | GateDecision、accepted/rejected Registry events |
| Automation | 两轮 loop、停止条件、one-time final、completion index | lineage、L3、portfolio、release manifest、安装校验输出 |

打开 [`starter/`](starter/) 开始。完成一个 milestone 后，只对照
[`solution/`](solution/) 中同名文件。不要提前复制后续 solution。

每个 milestone 还要实现统一的 `execute_target`。clean-room 会从
[`fixtures/milestone-policy-v1.json`](fixtures/milestone-policy-v1.json) 读取课程原创判断探针，
先调用该模块的 seed/grade/diagnosis/guardrail/lifecycle 等窄判断函数，再校验结果。校验通过后，
wrapper 才允许该 milestone 恰好执行一次它负责的 CLI。只写 `return execute_once()` 会在 CLI
执行前失败，不能产生毕业证据。

## 准备

在仓库根目录运行。fixed 路径无 Key、无网络：

```bash
uv sync --all-extras --locked --offline
PROFILE=course/capstone-shopping-assistant/profiles/fixed-v1.json
ROOT=.ses/shopping-capstone
REGISTRY="$ROOT/registry"
PAIRED_TRACE="$ROOT/run-shopping-develop-skill-v0-fixed/artifacts/shopping-develop-01/iteration-0/attempt-0/trace-turn-0001.json"
MANUAL_GATE_DECISION="$REGISTRY/gates/gate-shopping-manual/gate-decision.json"
REJECTED_GATE_DECISION="$REGISTRY/gates/gate-auto-r002/gate-decision.json"
INSTALL_ROOT="$ROOT/installed-skill"
```

先运行课程测试。显式选择 `starter` 后，测试会要求五个文件不再包含开放缺口，并运行锁定
policy probe。你完成后应让自己的实现测试转绿：

```bash
SES_CAPSTONE_IMPLEMENTATION_VARIANT=starter uv run --offline --frozen pytest -q course/capstone-shopping-assistant/tests
```

## Milestone 1 — Create

你先验证锁定 profile，再从八份原创 Creator projection 产生 learner v0。Static 必须拒绝
商品 ID、gold、选择/终局内容、危险工具和越权说明。Trigger 必须达到 10/10。

```bash
uv run --offline --frozen ses doctor --profile "$PROFILE"
uv run --offline --frozen ses skill create-v0 --profile "$PROFILE" --experiment-root "$ROOT"
uv run --offline --frozen ses skill static-gate --profile "$PROFILE" --experiment-root "$ROOT"
uv run --offline --frozen ses trigger-eval --profile "$PROFILE" --experiment-root "$ROOT"
```

检查 create、Static 和 Trigger receipt 中的输入 hash、输出 hash、主指标、费用、停止原因和
下一步命令。任何一项缺失都不能进入 Eval。

## Milestone 2 — Eval

两侧必须共享 task/profile/model/protocol，但使用不同 session、workspace、episode 和 Trace；
两侧都只运行 `iteration-0`。Evaluator 只评分 gateway 已执行的动作，不能重复执行工具。

```bash
uv run --offline --frozen ses paired-comparison --profile "$PROFILE" --experiment-root "$ROOT"
uv run --offline --frozen ses inspect paired-trace "$PAIRED_TRACE" --profile "$PROFILE" --experiment-root "$ROOT"
```

你要指出四种 scenario 的结果，并从同一 episode 追到原始 reward、metric projection 和
`CaseGrade`。review receipt 只证明你打开过证据；课程问题还会检查你的解释。

fresh pair 完成后，先用完整 v0 pipeline summary 初始化 Registry。它会把 learner v0 记为
当前 accepted；它不能从 reference fallback 自举：

```bash
uv run --offline --frozen ses registry init --profile "$PROFILE" --registry "$REGISTRY" --experiment-root "$ROOT" --initial-skill "$ROOT/skill/v0" --initial-evidence "$ROOT/v0-pipeline-summary.json"
```

## Milestone 3 — Evolve

先按 runtime → case/gold → Judge/Simulator → Skill 的顺序归因。只有 Skill 根因能产生
Patch；每个 add/update/delete 操作都要引用 Failure Card、Trace 和 assertion/safety evidence。

```bash
uv run --offline --frozen ses evolve --profile "$PROFILE" --experiment-root "$ROOT"
uv run --offline --frozen ses inspect failure-evidence "$ROOT/failure-evidence.json" --profile "$PROFILE" --experiment-root "$ROOT"
uv run --offline --frozen ses inspect failure-card "$ROOT/manual-evolution/failure-cards.json" --profile "$PROFILE" --experiment-root "$ROOT"
```

Patch 最多包含三个操作，只能修改候选 Skill runtime 文件。它不能修改 Adapter、Judge、
profile、split、Gate policy、预算、课程测试或源码。

## Milestone 4 — Gate

Registry 已在 fresh pair 后完成初始化。现在你只注册 learner candidate，再运行受保护 Gate。

```bash
uv run --offline --frozen ses registry register --profile "$PROFILE" --experiment-root "$ROOT" --registry "$REGISTRY" --candidate "$ROOT/manual-evolution"
uv run --offline --frozen ses gate candidate --profile "$PROFILE" --experiment-root "$ROOT"
```

接受和提升是两个事件。只有 `GateDecision.outcome == accepted` 时才设置候选 ID 并提升：

```bash
ACCEPTED_CANDIDATE_ID=candidate-from-the-manual-gate-output
uv run --offline --frozen ses registry promote --profile "$PROFILE" --experiment-root "$ROOT" --registry "$REGISTRY" --candidate-id "$ACCEPTED_CANDIDATE_ID" --gate-decision "$MANUAL_GATE_DECISION"
```

你必须解释一份 rejected GateDecision。未授权购买、安全违规、critical pass-to-fail、strict
回归、full-success 平局和不完整证据都要拒绝。

## Milestone 5 — Automation and release

手动分支完成后，自动循环至少执行两轮，并复现一次接受和一次拒绝或回滚。循环不能暗中
执行 final。final 只针对循环结束时的 current accepted 运行一次；它发现任何 safety
violation 都会把实验标记为 `failed_final` 并阻止 package。

```bash
uv run --offline --frozen ses auto-evolve --profile "$PROFILE" --experiment-root "$ROOT"
uv run --offline --frozen ses inspect gate-decision "$REJECTED_GATE_DECISION" --profile "$PROFILE" --experiment-root "$ROOT"
uv run --offline --frozen ses inspect registry-history "$REGISTRY/events.jsonl" --profile "$PROFILE" --experiment-root "$ROOT"
uv run --offline --frozen ses final --profile "$PROFILE" --experiment-root "$ROOT"
uv run --offline --frozen ses l3-render --profile "$PROFILE" --experiment-root "$ROOT" --output "$ROOT/l3.html"
uv run --offline --frozen ses portfolio-export --profile "$PROFILE" --experiment-root "$ROOT" --output "$ROOT/portfolio"
uv run --offline --frozen ses skill package --profile "$PROFILE" --experiment-root "$ROOT" --registry "$REGISTRY" --current-accepted --output "$ROOT/package"
uv run --offline --frozen ses capstone-index --profile "$PROFILE" --experiment-root "$ROOT" --output "$ROOT/capstone-index.json"
uv run --offline --frozen ses skill-install --accepted-package "$ROOT/package/release-manifest.json" --profile "$PROFILE" --experiment-root "$ROOT" --destination "$INSTALL_ROOT"
```

fixed 自动进化的第二轮会留下 rejected decision。你不能提升该候选；上面的 inspect 命令只会
写审阅 receipt，不会改变 Registry。

`CapstoneIndex` 会先重放标准实验路径并绑定五类 review receipt、手动 Gate/Registry、两轮
automation、唯一 final、L3、portfolio 和 accepted-only release manifest。安装是后续独立动作；
它会再次重放 Registry、final 和 release manifest，不能绕过 package eligibility。

## 当前 CLI 状态与验证

上面的命令是 spec 11 锁定的接口。capstone clean-room 会在新副本中逐条执行它们；任何
失败或未执行命令都会阻止 `workflow_complete`。默认 clean-room 运行你的 `starter`；它为
五个实现文件和每条 policy check 写入 hash，并绑定最终 `CapstoneIndex` hash。结构验证和
learner clean-room 命令：

```bash
uv run --offline --frozen python scripts/validate_capstone.py --root . --structure-only --json
uv run --offline --frozen python scripts/run_capstone_clean_room.py --source-root . --workspace /tmp/ses-shopping-capstone-clean --output /tmp/ses-shopping-capstone-evidence.json
```

课程维护者可以在另一个空 workspace 显式运行 reference solution，确认同一套判断探针和 22 条
命令可复现；这不会修改你的 starter：

```bash
uv run --offline --frozen python scripts/run_capstone_clean_room.py --source-root . --workspace /tmp/ses-shopping-capstone-reference --output /tmp/ses-shopping-capstone-reference-evidence.json --implementation-variant solution
```

clean-room 使用当前工作树文件，不依赖 `git archive HEAD`，因此未提交的开发态也能验证。
它清除凭据环境变量，并在 Phase 0 no-go 下把 live 记录为 `blocked`，不会执行 live 命令。
`milestone_implementations` 会记录五个实现 hash 和各自的 policy-check 汇总 hash；22 条
`target_commands` 仍保留 spec 锁定的原始 command ID 和 command hash。

## 费用、参考和提交物

fixed 新增费用为 0 CNY。课程预算口径和 live 未知项见 [`BUDGET.md`](BUDGET.md)。无 Key
参考边界见 [`REFERENCE.md`](REFERENCE.md)，拓展阅读见
[`FURTHER_READING.md`](FURTHER_READING.md)。

你最终提交：`CapstoneIndex`、五类 review receipts、L3、portfolio manifest、release manifest、
accepted install 的校验输出，以及一段解释接受/拒绝分支和 final 安全结果的短文。不要提交 private
selection/final 题面、逐题 reward、persona、上游资产、Key 或本机绝对路径。
