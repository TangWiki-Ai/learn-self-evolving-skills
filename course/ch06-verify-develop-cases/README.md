# 第 6 课：让 LLM 帮你造考卷，但不要让它写答案

## 困惑

候选日志或 benchmark 对话不是可执行案例。纯规则很难读懂大量自然语言；纯 LLM 又会误判意图、编造金额和给自己的题放行。你需要把两者放在正确的位置。

## 方法

本课走完一条缩小规模、不删除阶段的流程：

```text
source evidence
  -> deterministic signals
  -> LLM triage
  -> LLM wording/rubric draft
  -> controlled variant
  -> deterministic oracle
  -> environment replay
  -> Judge calibration
  -> course-authored attestation (pending human review)
  -> fixed/offline course catalog (not live-qualified)
```

默认运行使用固定模型响应，所以课堂和 CI 不需要网络。当前 15 条课程 case 还没有独立真人签名；fixed/offline 可以用它们演示完整协议，但 live 和 release 会在读取凭据或调用 Provider 前关闭。

## 关键 insight

LLM 是候选生成器和审核助手，不是事实来源。

它可以回答“这段对话是在发起退货，还是查询退款进度”，也可以起草“最终回复应准确解释实际结果”这样的 rubric。但 Shop policy 才能回答退款金额和终态。确定性能力门也必须保留：即使模型把 `refund_status` 判成可映射，Pipeline 仍要拒绝。

## 你会看到什么

`review-packet.json` 为每个变体展示：

- 精确 source turn 与 benchmark 版本
- 确定性 marker 和风险
- LLM triage、置信度、证据引用及 prompt/response provenance
- LLM 生成的公开请求模板和语义 rubric 草案
- 受控政策维度、oracle 摘要和 replay 状态
- 正确、错误、证据不足的 Judge 结果
- 当前 evidence binding、课程纳入/排除 attestation 和明确的待人工复核状态

Rubric 当前保持 `advisory_not_activated`。课程 attestation 只表达 fixed/offline 的课程选择，不代表人工批准，也不会激活 rubric。

## Starter

[`starter/qualification.py`](starter/qualification.py) 保留四类教学缺口：

1. `curate_candidate_sources`：读取来源证据，执行 fixed/live 共用的 triage 和 rubric schema。
2. `verify_variant`：验证政策组合并生成稳定、无答案泄漏的 case。
3. `calibrate_case`：证明正确、错误和证据不足得到预期状态。
4. `protect_split`：受信的持久化前 verifier 检查 source ID、semantic group、case ID 和
   content hash 四维重叠，但不向 Creator/Updater 返回 holdout 身份。

[`solution/qualification.py`](solution/qualification.py) 直接调用生产模块，不复制一套简化 policy、模型 parser 或 Judge。

## 运行

离线复现：

```bash
uv run ses qualify-cases --curation-mode fixed \
  --output .ses/lesson06-fixed --json
uv run ses baseline --run-id run-lesson-6-expanded --iterations 2 --json
uv run pytest course/ch06-verify-develop-cases/tests
```

默认 fixed/offline 命令显式使用 `fixed_offline_unverified` adapter，只演示课程流程，不声称已在
本次生成中重验受保护 holdout 的四维互斥。你可以用仓库外完整 bundle 加
`--protected-holdout-root PATH` 启用 commitment-verified 检查。`--curation-mode live` 缺少该
外部 verifier 时会先关闭；提供它后，当前仍会因缺少独立签名人审而在读取
`SILICONFLOW_API_KEY` 前关闭。未来 live 路径仍必须使用同一 schema、能力门、oracle 和 split
protection；凭据只能来自环境，不能进入日志或 artifact。

## Judge meta-eval

[`judge-meta-eval.json`](judge-meta-eval.json) 固定四个必须覆盖的组合：

| 状态 | 解释 | 预期 |
|---|---|---|
| 正确 | 正确 | `pass` |
| 正确 | 错误 | `fail` |
| 错误 | 话术漂亮 | `fail` |
| 证据不足 | 无法判断 | `not_evaluated` |

这个矩阵说明 composite rubric 的优先级：LLM 不能用漂亮话术覆盖错误业务状态；确定性状态正确也不能掩盖错误的用户说明。

## 对照产物与预算

- `qualification-funnel.json` 记录 source、selected source、15 条 fixed course case、15 条 pending 和 7 条课程排除；`qualified_count=0`。
- `expanded-baseline.json` 是 measured offline fixture execution，必须保留 `live_model_measured=false`。
- 固定 curation 的费用为 0，`network_used=false`。当前 live curation 因待人审状态而关闭。

## 拓展阅读

- 阅读 `docs/specs/05-testset-pipeline.md`。回答：为什么 LLM triage 之后仍需要确定性能力门？
- 阅读 `docs/specs/10-cross-module-contracts.md`。回答：为什么启用 rubric 草案时必须让旧审批失效？
- 阅读 Anthropic 的 [Agent eval 指南](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。对照本课找出代码、模型和人工三类 grader。
- 阅读 OpenAI 的 [GDPval 方法说明](https://openai.com/index/gdpval/)。回答：为什么自动 grader 可以提高审核效率，却不能自行成为 gold owner？
