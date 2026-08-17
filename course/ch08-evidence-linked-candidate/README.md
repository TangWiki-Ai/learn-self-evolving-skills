# 第 8 课：生成有证据链接的候选 Skill

本课把失败评测转成可审核的 Failure Card，再用小型结构化补丁生成不可变候选。

流程固定为：

```text
脱敏失败 evidence
  → 固定归因：runtime/environment → case/gold → Judge/Simulator → Skill
  → 六类 Failure Card
  → add / update / delete PatchOperation
  → schema + patch validation + Ticket 08 Static Gate
  → 新的 candidate Skill
```

## 边界

Updater workspace 只包含脱敏失败 fixture 和 accepted parent 的可安装 Skill 文件。
它不包含源码、凭据、gold、selection/final 数据、Judge 私有材料或 provider stream。
候选创建永远读取 parent 的 hash，并把结果物化到新的目录；它不会原地修改 parent。

`tests/fixtures/evolution/live-failure-evidence.json` 来自 Ticket 08 live paired
artifact 的最小导出。它保留了比较、pair execution、事件日志和 v0 的哈希，删除了
原始 provider stream、绝对路径、订单/客户标识、金额、模型私有内容。这个 live fixture
包含 3 个 `infrastructure_error`，所以分析器拒绝 Skill patch，也不把它们改写成六类
教学失败。

`artifacts/synthetic-failure-cards.json`、`artifacts/evidence-linked-patch.json`
和 `artifacts/evidence-linked-patch-list.json` 明确标记为 synthetic。它们只用于展示
六类分类和 add/update/delete 的离线教学协议，不能冒充 live provenance。

## 运行离线 vertical slice

```bash
uv run ses candidate-patch \
  --parent course/ch07-create-v0/artifacts/skill/v0 \
  --evidence tests/fixtures/evolution/synthetic-failure-evidence.json \
  --patch course/ch08-evidence-linked-candidate/artifacts/evidence-linked-patch.json \
  --failure-cards course/ch08-evidence-linked-candidate/artifacts/synthetic-failure-cards.json \
  --output .ses/lesson-08/candidate \
  --record-output .ses/lesson-08/candidate.json \
  --parent-sha256 a19c423b65f9ef7960d682045832f7a8bf57fbbda759a42e102cb28ddfc8ef26
```

命令只运行本地 schema、证据、补丁、candidate 和 Static Gate。它不会启动 Provider。

## Starter 与 solution

`starter/evolution.py` 保留失败分析和 candidate 创建的实现缺口；
`solution/evolution.py` 直接调用 `ses.evolution` 的生产逻辑。运行课程测试：

```bash
uv run pytest course/ch08-evidence-linked-candidate/tests
```
