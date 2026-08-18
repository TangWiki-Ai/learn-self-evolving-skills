# 第 10 课：有界自动进化与公开作品集

本课把已经验证的 rollout、失败归因、Updater、Gate 和 Registry 串成两轮有界循环。
循环停止后，系统只对 current accepted Skill 运行一次 final，再导出 L3 和作品集。

## 困惑

把几条命令放进 `while True` 不等于安全自治。循环可能重复付费、围绕噪声修改、跳过
Gate，或把 final 的逐题结果当成下一轮提示。这样得到的“提升”无法独立验证。

## 方法

生产编排器固定执行：

```text
current accepted Skill
  -> fresh develop rollout -> reflect -> bounded patch
  -> Issue #10 shared Gate -> Registry decision -> accept/promote or reject
  -> round/token/cost/rejection/cooldown/convergence stop
  -> one-time final aggregate
  -> L3 capability/cost curve -> allowlisted portfolio
```

每个可能付费的步骤先写 intent。恢复时，完整产物直接复用；只有 intent、没有完整产物时，
系统停止并要求人工判断，不会盲目重放。只有完整且 accepted 的 `GateDecision` 能改变
Registry 指针。

## 业界做法与关键 insight

可靠的自动优化把搜索器和裁判分开：修改流程提出 candidate，独立 Gate 决定是否接受。
本项目进一步锁定 split、模型、协议和预算。Final 在循环结束后只返回汇总结果，不向
Creator、Updater 或下一轮提供逐题信号。

关键 insight：自动化不会提高证据质量。它只能重复已经可信的步骤。因此 Judge、数据或
Registry 完整性异常必须让循环停止。

## 5 分钟 fixed/offline 路径

下面的命令不读取 Key、不访问网络，实际新增付费为 `0 CNY`。它使用 synthetic fixture 复现两轮，
第一轮接受并提升，第二轮平局拒绝；随后生成一份 12 题 fixed reference final 汇总。它不是
canonical live 测量。

```bash
uv run ses auto-evolve \
  --mode fixed \
  --output-root .ses/lesson-10 \
  --json

uv run python course/ch10-auto-evolve-and-portfolio/scripts/export_result.py \
  --experiment .ses/lesson-10 \
  --l3 .ses/lesson-10-l3.html \
  --portfolio .ses/lesson-10-portfolio
```

第一条命令可安全重复运行：它验证并复用同一实验，不重复追加 Registry event，也不重复
运行 final。第二条命令要求 portfolio 目标尚不存在，避免覆盖已有审计包。

## Starter、实现任务与 solution

[`starter/automation.py`](starter/automation.py) 保留四个学习缺口：

1. 串联有界循环并实现恢复；
2. 从聚合记录构建 L3 版本 DAG 和能力/成本曲线；
3. 用 allowlist 导出 accepted Skill、Registry/Gate 公共投影和 final 汇总；
4. 证明 final 不会进入 Patch 或下一轮。

[`solution/automation.py`](solution/automation.py) 直接调用生产模块。它不复制 Gate、Registry
或 final 规则。

运行本课测试：

```bash
uv run pytest course/ch10-auto-evolve-and-portfolio/tests
```

## 对照产物

[`artifacts/fixed-reference/`](artifacts/fixed-reference/) 是生成器实际产出的公开审计包：

- `l3.html`：单文件、无外链资源，展示版本 DAG、拒绝分支和能力/成本曲线；
- `final-aggregate.json`：只含 12 题汇总，不含逐题结果或可枚举的逐题结果哈希；
- `registry/events-public.json` 与 `gate-projections/`：保留聚合谱系，不复制私有 evidence 路径；
- `accepted-skill/`：只含 manifest 声明的 current accepted Skill 文件；
- `manifest.json`：每个公开成员的 SHA256 allowlist。

维护者可以先在全新目标目录重新生成并审核：

```bash
uv run python course/ch10-auto-evolve-and-portfolio/scripts/generate_fixed_reference.py \
  --output-root .ses/lesson-10-reference
```

生成器不会覆盖已有目录。需要重生签入的对照包时，维护者必须明确传入
`--output-root course/ch10-auto-evolve-and-portfolio/artifacts/fixed-reference`，且目标目录必须
事先不存在。

测试会在两个临时目录重复生成，并比较 portfolio semantic hash。签入产物明确标记
`fixed_offline_reference`，不能替代学习者的 fresh run，也不能冒充 SiliconFlow live final。
当前固定包的 semantic hash 是
`e17b6531f685d87cc498a5deac98cbea473018ec84a66c24e9a826c9bc06db88`。

## 预算与限制

| 路径 | 费用口径 | 本课结果 |
| --- | --- | --- |
| fixed/offline artifact | fixed，零网络 | 记录 synthetic 参考成本 `0.02460 USD` |
| 本次实际付费 | measured spend | 实测 `0 CNY` |
| canonical live | measured，SiliconFlow 锁定模型 | 本课未运行，不提供或推断结果 |

公开 portfolio 不包含 selection/final 私有题面、逐题结果、参考轨迹、修改反馈、凭据或本机
绝对路径。仓库内 fixed final 只用于理解协议结构。你要提交结课 live 证据时，必须在锁定
Provider 和私有 runner 可用后声明新实验。

## 拓展阅读

- 阅读 [`docs/specs/08-automation-portfolio.md`](../../docs/specs/08-automation-portfolio.md)
  的 Implementation Decisions。回答：为什么自动模式不能直接 promote？
- 阅读 [`docs/specs/07-evolution-governance.md`](../../docs/specs/07-evolution-governance.md)
  的 Registry 与 rollback 部分。回答：为什么拒绝分支也要保留？
- 阅读 [`docs/specs/10-cross-module-contracts.md`](../../docs/specs/10-cross-module-contracts.md)
  的序列化规则。回答：公开 portfolio 为什么使用投影，而不是直接复制私有 Gate evidence？
