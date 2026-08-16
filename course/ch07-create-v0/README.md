# 第 7 课：从 9 条成功轨迹创建 Skill v0

## 你要解决什么

一份看起来合理的 `SKILL.md` 不能证明 Skill 有效。Creator 可能抄下案例标识或固定答案；description 可能漏触发，也可能污染无关任务；历史 baseline 还可能让 before/after 失去可比性。

本课把流程拆成五个可审计关口：

```text
9 条 creator 成功轨迹
  → 隔离 Creator workspace
  → Static Gate（零成本）
  → Claude Code 原生 Trigger Eval（10 正 + 10 负）
  → 15 条 develop fresh paired comparison
  → 单文件 L2 报告
```

## 关键边界

Creator 只看到允许公开的 9 个 seed projection 和 Skill 规范。它看不到完整源 Trace、Ticket 07 的 15 条 develop cases、selection、final、gold、失败证据、项目源码或凭据。默认 `FakeV0Creator` 固定输出，方便你离线调试；只有显式 live 命令才调用锁定的 Creator 模型。

Static Gate 必须先运行。失败候选保留 `static-gate.json`，但不能安装、不能进入 Trigger Eval，也不能启动 paired run。安装器只复制 manifest 中的 `SKILL.md` 和 `references/`；即使 artifact 目录放了 `eval/`、Trace 或其他诱饵文件，它们也不会进入 Agent workspace。

Trigger Eval 测的是 Claude Code 原生 Skill discovery，不是课程自建的关键词路由。默认结果回放固定的原生发现观察；显式 live 集成测试会允许 Claude Code 的 `Skill` 工具并观察真实 tool call。

## Starter 与 solution

[`starter/skill_v0.py`](starter/skill_v0.py) 留下四个实现缺口：seed 验证、Static Gate、Trigger Eval 和 paired comparison。它们对应本课四个判断 seam。

[`solution/skill_v0.py`](solution/skill_v0.py) 直接调用生产协议。它不复制 schema、Judge 或 Runner 逻辑。

运行课程测试：

```bash
uv run pytest course/ch07-create-v0/tests
```

## 完整离线 vertical slice

```bash
uv run ses skill-v0-pipeline --output-root .ses/lesson-07 --json
```

你也可以分步运行：

```bash
uv run ses skill create-v0 --out .ses/lesson-07/v0 --json
uv run ses skill static-gate --skill .ses/lesson-07/v0 --output .ses/lesson-07/static-gate.json --json
uv run ses trigger-eval --skill .ses/lesson-07/v0 --output .ses/lesson-07/trigger-eval.json --json
uv run ses paired-comparison --skill .ses/lesson-07/v0 --output-root .ses/lesson-07/paired --output .ses/lesson-07/comparison.json --json
uv run ses l2-render --comparison .ses/lesson-07/comparison.json --trigger .ses/lesson-07/trigger-eval.json --output .ses/lesson-07/l2.html
```

## 固定参考结果

[`artifacts/l2.html`](artifacts/l2.html) 是可离线打开的 fixed/offline reference。它运行真实的 Runner、独立 Shop、Trace、StateDiff 和 CaseGrade，但 Agent 行为来自确定性 fake engine，所以不能冒充 live 模型质量。

固定定量结果：

- Creator seed：9/9 通过 State Judge、模型 Judge 和人工审核。
- Trigger：TP=10、FP=0、TN=10、FN=0，precision=1.00，recall=1.00。
- Paired：15 case；fail-to-pass=1、pass-to-fail=1、both-fail=1、both-pass=12。
- Baseline pass rate=13/15（86.7%）；Skill v0 pass rate=13/15（86.7%）。这份 fixture 用四种翻转教学配对分析，不声称净提升。
- Skill 侧增加固定上下文开销；报告同时展示 token、费用和耗时差异。

## Fixed 与 live

`fixed/offline` 用固定响应和 fake engine，网络使用为零，结果可复现。`live measured` 必须显式启用、从 `models.lock.json` 读取角色模型，并从进程环境读取 `SILICONFLOW_API_KEY`。Live artifact 写到临时目录，不覆盖这里的参考结果。

不要把两类数字混在一起。固定结果证明协议和课程代码可重放；live 结果才描述当前 Provider、模型和 Claude Code 版本的实际行为。
