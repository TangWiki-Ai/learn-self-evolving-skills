# 第 1 课：先看见 Skill 的差别

## 困惑

你给 Agent 一份长提示词，再看到它成功一次，很容易得出“Skill 有效”的结论。但两次运行如果用了不同 case、不同 fixture 或缓存，这个差别没有意义。生成器也可能给你一份很短、结构错误或无法安装的候选，课程不能假装它可用。

## 方法

你在同一个固定退货 benchmark case 上运行两次 fresh conversation。第一次不安装 Skill。第二次把 manifest 声明的 Skill 文件安装到新的 workspace。两边使用同一个离线 Engine、同一工具协议和新的 Shop 环境；Engine 根据 workspace 里实际安装的 Skill 决定动作。

运行默认离线生成候选：

```bash
uv run ses skill-demo --generate --output-root .ses/lesson-1-demo
```

你也可以指定自己的候选，或明确使用 reference：

```bash
uv run ses skill-demo --candidate ./my-skill --output-root .ses/lesson-1-demo
uv run ses skill-demo --reference --output-root .ses/lesson-1-reference
```

需要机器可读产物时加 `--json`。候选内容弱、结构错误或不可安装时，流程才使用 reference fallback，并在 comparison artifact 中记录原因。

## 业界做法

成熟的 Agent 评测会锁定 case、工具、模型配置和协议，再做 paired run。Skill 安装器采用 allowlist：manifest 明确列出运行文件及 SHA-256，安装后再核对文件清单、hash、符号链接和路径。这个做法与软件供应链的“声明—验证—安装”模式相同。

## 关键 insight

fixture 只能描述离线世界，不能替你决定实验结论。真正的自变量是 workspace 里有没有适用 Skill。无关 Skill 不能让 case 自动通过。

## Starter

[`starter/skill_choice.py`](starter/skill_choice.py) 留下一个明确缺口。你需要选择安全的生成候选；候选不可用时，选择 reference 并标记 fallback。

## 实现任务

1. 阅读 [`creator-prompt.md`](creator-prompt.md)，列出 Creator 看不到的材料。
2. 阅读 [`reference-skill/skill-manifest.json`](reference-skill/skill-manifest.json)，核对每个声明文件的 SHA-256。
3. 完成 `starter/skill_choice.py`，保留候选或 fallback 原因。
4. 分别运行 `--candidate` 和 `--reference`，观察 comparison 中的 `skill.source`。
5. 检查两个 Trace 的 prompt、allowed tools、timeout 和 protocol hash 是否一致。
6. 确认 with-Skill workspace 只包含 manifest 声明的 `SKILL.md` 和 references。

## 测试

```bash
uv run pytest course/ch01-see-the-difference/tests
```

测试会检查 solution、starter 的预期缺口、严格 comparison schema、reference manifest，以及 Lesson 1 产物到 Lesson 2 状态证据的课程链。

## 对照产物

[`comparison-artifact.json`](comparison-artifact.json) 使用和当前运行相同的严格 schema。它标记 `measured: false` 和 `source.kind: checked_in_reference`，只供离线阅读。你自己运行得到的产物标记 `measured: true` 和 `source.kind: current_run`。

你要观察：

- 两个 run ID、workspace 和 Trace 都不同。
- 两边 case、prompt、工具和 timeout 相同。
- baseline 没有安装 Skill；with-Skill Trace 记录完整可安装内容的 hash。
- comparison 同时展示消息、工具调用、终态和 fallback 原因。
- 单个固定 case 只展示定性差异，不证明稳定提升。

下一课会读取这些 Trace 和 StateDiff：[第 2 课：从终态给一个 case 判分](../ch02-grade-terminal-state/README.md)。

## 拓展阅读

- 阅读 [`docs/specs/06-skill-creation-triggering.md`](../../docs/specs/06-skill-creation-triggering.md) 的安装器与 paired comparison 部分。回答：为什么 baseline 不能复用卸载 Skill 后的缓存？
- 阅读 [`docs/specs/10-cross-module-contracts.md`](../../docs/specs/10-cross-module-contracts.md) 的 serialization rules。回答：为什么 artifact 需要 `schema_version`、`record_type` 和相对路径？
- 阅读 [Anthropic Agent Skills 概览](https://docs.anthropic.com/en/docs/agents-and-tools/agent-skills/overview) 的 Skill 发现与目录结构部分。回答：description 为什么同时影响误触发和漏触发？

## 预算

默认路径使用 FakeCreator、离线 Engine 和本地 Shop MCP，预计费用和实测费用都是 0 元，不读取 Key。你只有在后续课程显式运行 live exercise 时才会产生模型费用。Lesson 1 的时间预算约 20 分钟：阅读 8 分钟，实现 7 分钟，运行与核对 5 分钟。

## 安全边界

安装器只复制 manifest 声明的 `SKILL.md` 和 `references/` 普通文件。它拒绝 hash 不匹配、路径逃逸、符号链接和安装后清单漂移。eval、gold、Trace、隐藏材料和未声明文件不会进入 Agent workspace。
