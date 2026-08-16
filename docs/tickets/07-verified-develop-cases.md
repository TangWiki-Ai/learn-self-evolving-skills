# Ticket 07：LLM 辅助构建 verified develop cases

## 目标和边界

本 Ticket 让学习者走完一条轻量但结构完整的评测集构建流程：从 benchmark proxy 中找失败信号，让 LLM 辅助筛选和起草 rubric，再由确定性环境生成 gold，最后经过重放、Judge 校准和人工审核进入 develop。

课程固定使用 Issue #6 的小型 candidate 输入，不下载完整上游仓库。ABCD 提供表达和意图证据；STATE-Bench Shop policy `state-bench-customer-support-return-v1` 提供可执行环境和 oracle；tau2 难度只保留在 candidate lineage。课程不把这些材料称为线上生产日志。

真实系统会把同一个 `source evidence` 接口接到脱敏 Trace、工具报错、人工接管、低评分和状态异常等线上信号。课程用固定 benchmark proxy 替代这一步的数据接入，但保留后面的筛选、起草、验证和人审职责，不把“没有生产流量”误写成“行业流程不需要 LLM”。

## 流程

每条 source candidate 按下面顺序处理：

1. **Source evidence**：从固定 ABCD 版本读取 delexed 对话，并保留 source ID、flow、subflow 和精确 turn。
2. **Deterministic signals**：提取已知意图 marker，并检查当前 Shop 是否支持该 flow/subflow。这个便宜的能力门可以否决模型误判。
3. **LLM triage**：模型返回严格 JSON：`intent`、`failure_type`、`mappable`、`severity`、`evidence_spans`、`confidence`、`reason`。每个 evidence span 必须逐字对应一个 source turn。
4. **LLM rubric draft**：只对通过筛选的 source 起草公开请求模板和基于 `tool_timeline`、`key_messages` 的语义 criterion。模型不能提供金额、终态、oracle 或 gold。
5. **Controlled variant**：Generator 只改变 Shop schema 支持的会员、Prime、退货窗口、交付天数、原因、金额和补货费维度。
6. **Deterministic oracle + replay**：政策引擎计算金额和终态；fresh `CaseEnvironment` 执行标准工具序列并对账。
7. **Judge calibration**：正确、错误和证据不足答卷必须分别得到 `pass`、`fail`、`not_evaluated`。课程还提供跨层 meta-eval，覆盖“状态正确但解释错误”和“状态错误但话术漂亮”。
8. **LLM-assisted human review**：集中 packet 同时展示来源证据、模型筛选、rubric 草案、变体、oracle 摘要、replay 和 Judge 结果。只有 owner 对同一 case reviewed hash 的批准才能入库。

ABCD 3592 明确表达“发起退货”，通过 source gate。ABCD 9489 查询已经存在的退款进度；模型和确定性能力门都拒绝把它改写成“发起退货”。原 7 条相关 rejection 继续保留在正式审计记录中。

## 模型与人的职责

- LLM 负责缩小阅读范围、解释意图、引用证据、起草公开表达和语义 rubric。
- 代码负责 schema、能力边界、金额、政策、终态、重放、hash 和 split protection。
- 人负责确认来源映射和题目语义。模型不能批准自己的产物。

当前 15 条 case 的既有人工审批绑定题面、oracle、replay 和 Judge calibration；这些内容没有改变，因此 Pipeline 不伪造新审批。新增 rubric 明确标记为 `advisory_not_activated`，不会偷偷改变当前评分。以后若把它激活为正式 model-scored rubric，必须把 rubric hash 纳入 review binding 并重新审核。

## 公开依据与课程取舍

- Anthropic 的 [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) 建议从手工检查、缺陷单和支持请求中的真实失败开始，组合代码、模型和人工 grader，并用专家判断校准 LLM grader。
- OpenAI 的 [GDPval 方法说明](https://openai.com/index/gdpval/) 让领域专家编写真实任务、参考产物和 rubric，再用自动 grader 估计专家判断；自动 grader 不替代专家。
- OpenAI 的 [AgentKit Evals 说明](https://openai.com/index/introducing-agentkit/) 把数据集、自动 grader、人工标注和 trace grading 放在同一套迭代流程中。

Ticket 07 保留这些职责和关卡，但缩小数据规模。固定 ABCD 对话代替生产日志接入，签入的模型响应代替 CI 中反复付费调用；显式 `live` 模式让学习者真正调用一次模型。课程没有实现持续日志采样、多人标注一致性、线上漂移监控和大规模抽检，这些属于后续生产化扩展。

## Fixed 与 live

默认命令回放签入的模型响应，供 CI 和课堂稳定复现：

```bash
uv run ses qualify-cases --curation-mode fixed --json
```

显式 live 模式通过 ClaudeCLI 调用 `models.lock.json` 中的 Judge 角色做 triage、Creator 角色起草 rubric：

```bash
SILICONFLOW_API_KEY=... uv run ses qualify-cases \
  --curation-mode live \
  --curation-timeout 120 \
  --json
```

凭据只从进程环境读取。产物只记录 provider host、model ID、model lock hash、prompt version/hash、response hash、token 和耗时，不保存原始 Key。live 输出仍经过与 fixed 相同的 JSON parser、能力门、oracle、replay 和人工审核规则。模型改变公开题面时，旧 reviewed hash 自动失效。

## Split 与可见性

- `selection` 和 `final` manifest 标记 `locked=true`；Pipeline 在创建输出前拒绝写入。
- 入库前检查 case ID、公开内容 hash 和来源语义组。
- Agent 只看到公开 CaseDefinition 和当前工具结果。source evidence、triage、rubric 草案、oracle 和审核记录不进入 Agent prompt 或 L1 HTML。
- `data_version` 覆盖 curation manifest、fixture、expected actions、policy version、qualification hash 和 qualification manifest。

## Baseline

```bash
uv run ses baseline \
  --output-root course/ch06-verify-develop-cases/artifacts \
  --run-id run-ticket07-expanded \
  --iterations 2 \
  --json
```

该 baseline 仍是 `fixed_response` 的可执行离线回归：它实际运行 Runner、Shop、Trace、StateDiff、State/Rule Judge、CaseGrade 和 L1 renderer，但不代表 live agent 质量。Ticket 07 的 live 调用用于体验评测集 curation；真实 agent baseline 需要单独标记 `live_model_measured=true`，不能用这份离线结果冒充。

## Canonical artifacts

- `data/testset/ticket07/curation-responses.json`：可复现的固定 triage/rubric 响应。
- `data/testset/ticket07/generated/curation-manifest.json`：source 筛选漏斗、provenance、usage 和私有 artifact 引用。
- `data/testset/ticket07/generated/review-packet.json`：人工审核所需的完整集中 packet。
- `data/testset/ticket07/generated/develop-manifest.json`：15 条 qualified develop cases。
- `data/testset/ticket07/generated/qualification-manifest.jsonl`：15 条 qualified 与 7 条历史 rejected 审计记录。
- `course/ch06-verify-develop-cases/judge-meta-eval.json`：四种跨层 Judge 校准预期。
- `course/ch06-verify-develop-cases/expanded-baseline.json`：离线实测指标和 artifact digest。
