# 第 6 课：先验证考卷，再扩展 develop

## 困惑

候选表达不是可执行案例。你还不知道政策组合是否合法、终态是否正确、Judge 能不能区分正确和错误答卷，也不知道新题会不会泄漏到锁定 split。

## 方法

本课把固定候选依次变成受控变体、政策 oracle、环境重放和 Judge 正反校准。系统生成集中 review packet，等待真实 owner 审核。只有 replay、calibration 和人工批准都通过，Pipeline 才把案例写入 develop manifest。

## 关键 insight

Gold 不是文本答案。当前 Shop policy 对同一 fixture 做确定性计算，标准工具操作再用独立环境重放。两者终态一致，才能证明 case 可执行。人工审核也必须绑定被审核版本的 hash；内容一变，旧批准自动失效。

## Starter

[`starter/qualification.py`](starter/qualification.py) 保留三处教学缺口：

1. `verify_variant`：验证 schema 支持的政策组合并生成稳定身份。
2. `calibrate_case`：证明正确、错误和证据不足分别得到 pass、fail、not_evaluated。
3. `protect_split`：在写文件前检查 ID、内容 hash 和来源语义重叠。

## Solution

[`solution/qualification.py`](solution/qualification.py) 直接调用生产实现。它不会复制一份简化 policy 或 Judge。

## 运行

```bash
uv run ses qualify-cases --json
uv run ses baseline --run-id run-lesson-6-expanded --iterations 2 --json
uv run pytest course/ch06-verify-develop-cases/tests
```

`qualify-cases` 默认完全离线，不读取 Key。没有 owner 审核时，它保留 pending 记录并生成 `data/testset/ticket07/generated/review-packet.json`，不会伪造 qualified。

## 对照产物

- [`qualification-funnel.json`](qualification-funnel.json) 记录候选、pending、qualified 和 rejected 数量。
- `expanded-baseline.json` 在真实审核完成、15 个案例入库并实际运行 Runner 后生成。

对照运行使用 `fixed_response` FakeEngine。它属于 measured offline fixture execution，不是 live model 实测。报告必须保留 `live_model_measured=false`。

## 预算

默认路径费用为 0：不访问网络，不调用付费 Provider。live Provider 不属于本 Ticket 的默认验收。

## 拓展阅读

- 阅读 `docs/specs/05-testset-pipeline.md` 的 Verify、Calibrate、Split 段落。回答：为什么候选列表不能直接成为 develop？
- 阅读 `docs/specs/10-cross-module-contracts.md` 的 hash 和 artifact reference 规则。回答：人工批准为什么必须绑定内容 hash？
