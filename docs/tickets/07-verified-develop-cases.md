# Ticket 07：候选到 verified develop cases

## 固定输入与范围

本 Ticket 使用 Issue #6 的固定小型 candidate 输入，不下载完整上游仓库。ABCD 只提供表达和意图信号；STATE-Bench Shop policy `state-bench-customer-support-return-v1` 负责执行与 oracle。tau2 信号只保留在 candidate lineage，不进入 Shop replay。

`data/testset/ticket07/variant-plan.json` 只配置当前 Shop schema 支持的维度：会员等级、Prime、退货窗口、交付天数、退货原因、商品与订单金额、补货费。Generator 拒绝未知枚举、负数、订单小计低于商品价格，以及非 `changed_mind` 场景中的补货费。

## Qualification 协议

每条 active case 依次经过：

1. `candidate`：保留 candidate ID、source ID、semantic group 和 transformation version。
2. `replay_verified`：政策引擎计算金额和终态；fresh `CaseEnvironment` 执行标准工具序列并与 oracle 对账。
3. `judge_calibrated`：故意正确、错误和证据不足答卷必须分别得到 `pass`、`fail`、`not_evaluated`。固定模型响应标记为 `fixed_response`，不声称 live model。
4. `human_review_pending`：review packet 绑定公开题面、oracle/replay/calibration hash。
5. `qualified` 或 `rejected`：只有 owner 对同一 reviewed hash 的批准才能进入 develop。

Owner 首轮批准 8 条，因 ABCD `refund_status` 与“发起退货”不匹配而拒绝 7 条。Pipeline 保留这 7 条正式 rejection。替代案例来自明确表达“发起退货”的 ABCD 3592，并重新生成完整 oracle、replay、Judge 校准和新 reviewed hash；owner 随后批准全部 7 条。

## Split 与可见性

- `selection` 和 `final` manifest 标记 `locked=true`；qualification 在创建任何输出文件前拒绝写入这两个 split。
- 入库前检查 case ID、公开内容 hash 和来源语义组。
- develop manifest 只引用经过批准的 fixture、公开 CaseDefinition 和 expected actions。
- oracle、人工审核与校准私有字段不进入 Agent prompt、Trace 或 L1 HTML。
- `data_version` 覆盖实际 fixture、expected actions、policy version、qualification hash 与 qualification manifest digest。

## 离线命令

```bash
uv run ses qualify-cases --json
uv run ses baseline \
  --output-root course/ch06-verify-develop-cases/artifacts \
  --run-id run-ticket07-expanded \
  --iterations 2 \
  --json
```

两个命令默认不读取 API Key、不访问网络、不调用 live Provider。baseline 使用动态生成的 `fixed_response` FakeEngine fixture，但仍通过真实 Runner、Shop、Trace、StateDiff、State/Rule Judge、CaseGrade 和 L1 renderer。

## Canonical artifacts

- `data/testset/ticket07/generated/develop-manifest.json`：15 条 qualified develop cases。
- `data/testset/ticket07/generated/qualification-manifest.jsonl`：15 条 qualified 和 7 条历史 rejected 审计记录。
- `data/testset/ticket07/generated/review-packet.json`：当前 active case 的审核摘要。
- `course/ch06-verify-develop-cases/expanded-baseline.json`：离线实测来源、版本、指标和 artifact digest。
- `course/ch06-verify-develop-cases/artifacts/run-ticket07-expanded/l1.html`：自包含 L1 报告。
