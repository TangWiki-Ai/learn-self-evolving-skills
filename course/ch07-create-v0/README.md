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

Creator 只看到允许公开的 9 个 seed projection 和 Skill 规范。它看不到完整源 Trace、Ticket 07 的 15 条 develop cases、selection、final、gold、失败证据、项目源码或凭据。系统先在固定 STATE-Bench commit 上真实重放 9 条来源轨迹，核对每个工具返回和 StateDiff，再执行 State Judge 和锁定模型 Judge。课程作者 attestation 只绑定这条证据链，状态明确写成 `course_authored_pending_human_review`，不代表人工批准。默认 `FakeV0Creator` 可以用这组固定证据演示协议；live Creator 必须等独立人工签署 [`human-review-packet.md`](../../docs/release/human-review-packet.md)，否则 CLI 在 Provider 调用前关闭。

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
uv run ses skill-v0-pipeline --output-root .ses/lesson-07-pipeline --json
```

你也可以分步运行。下面整段使用另一个 fresh output root，不会与 vertical slice
生成的文件碰撞：

```bash
uv run ses skill create-v0 --out .ses/lesson-07-step/v0 --json
uv run ses skill static-gate --skill .ses/lesson-07-step/v0 --output .ses/lesson-07-step/static-gate.json --json
uv run ses trigger-eval --skill .ses/lesson-07-step/v0 --output .ses/lesson-07-step/trigger-eval.json --json
uv run ses paired-comparison --skill .ses/lesson-07-step/v0 --output-root .ses/lesson-07-step/paired --output .ses/lesson-07-step/comparison.json --json
uv run ses l2-render --comparison .ses/lesson-07-step/comparison.json --trigger .ses/lesson-07-step/trigger-eval.json --artifact-root .ses/lesson-07-step/paired --output .ses/lesson-07-step/l2.html
```

## 固定参考结果

[`artifacts/l2.html`](artifacts/l2.html) 是可离线打开的 fixed/offline reference。它运行真实的 Runner、独立 Shop、Trace、StateDiff 和 CaseGrade，但 Agent 行为来自确定性 fake engine，所以不能冒充 live 模型质量。

固定定量结果：

- Creator seed：9/9 通过真实 replay、State Judge 和签入的固定模型证据检查；9/9 人工复核仍待签署。
- Trigger：TP=10、FP=0、TN=10、FN=0，precision=1.00，recall=1.00。
- Paired：15 case；fail-to-pass=0、pass-to-fail=0、both-fail=0、both-pass=15。
- Baseline 和 Skill v0 都是 15/15。两侧使用同一 deterministic fake 行为，费用都是 0；系统没有伪造翻转来制造教学效果。
- 这组数字只证明 Runner、Judge、证据校验和 L2 可重放，不代表 Skill 提升了真实模型质量。

## Fixed 与 live

`fixed/offline` 用固定响应和 fake engine，网络使用为零，并规范化 Trace 事件时钟，所以相同输入会生成相同 artifact hash。当前 creator seed 和 develop packet 都仍待独立人工复核。`skill create-v0 --mode live`、`skill-v0-pipeline --mode live` 与 `paired-comparison --mode live` 会分别检查所需 creator/develop 人审记录，并在读取 Key 或启动 Provider 前关闭。签署集中复核包后还需要把可验证的签名记录接入 loader；在此之前，三条 live 路径都不可运行，也不要把 fixed Skill 转称为 live-created。未来生成的 Live artifact 必须写到独立临时目录，不能覆盖这里的参考结果。

不要把两类数字混在一起。固定结果证明协议和课程代码可重放；live 结果才描述当前 Provider、模型和 Claude Code 版本的实际行为。

## 拓展阅读

- [`06-skill-creation-triggering.md`](../../docs/specs/06-skill-creation-triggering.md)：回答 Creator 隔离、Static Gate、原生 Trigger discovery 和安装边界。
- [`10-cross-module-contracts.md`](../../docs/specs/10-cross-module-contracts.md)：回答 9 条 creator、15 条 develop 与后续 selection/final 之间怎样传递 hash，而不传递私有内容。
