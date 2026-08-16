# 第 1 课：先看见 Skill 的差别

## 目标

你在同一个固定退货 benchmark case 上运行两次：一次不安装 Skill，一次安装 demo Skill。两次运行都使用新的 workspace、新环境和新 Trace。你先看消息、工具调用和终态，再决定 Skill 是否改变了行为。

```bash
uv run python -m ses.cli.skill_demo --output-root .ses/lesson-1-demo
```

需要机器可读产物时加 `--json`。命令默认使用离线 `FakeCreator` 和 FakeEngine，不联网，也不读取 Key。Creator 失败时，命令会使用 `reference-skill/` 中明确标记的 reference Skill，并在 comparison artifact 中写出 fallback 原因。

## 你要观察什么

- 两个 run ID、workspace 和 Trace 都不同。
- 两边的 case、协议和模型 lock 相同。
- with-Skill Trace 记录完整可安装内容的规范化 SHA-256。
- comparison artifact 同时展示两边的消息、工具调用和状态结果。
- 本课只展示一个固定 case 的定性差异。它不证明稳定提升；更大样本和 paired comparison 属于后续课程。

## 练习

1. 阅读 [`creator-prompt.md`](creator-prompt.md)，圈出 Creator 不能看到的材料。
2. 阅读 [`reference-skill/SKILL.md`](reference-skill/SKILL.md)，说明它为什么可以兜底。
3. 完成 `starter/skill_choice.py`：生成候选可安装时使用它，否则选择 reference，并明确标记 fallback。
4. 运行测试：

   ```bash
   uv run pytest course/ch01-see-the-difference/tests
   ```

5. 对照 [`comparison-artifact.json`](comparison-artifact.json)。它是离线演示产物，`measured` 为 `false`。

## 安全边界

安装器只复制 `SKILL.md` 和 `references/` 下的普通文件。它不复制 eval、gold、Trace、隐藏文件或符号链接。它不接受候选目录之外的路径，也不会把已有 workspace 当成 baseline 缓存。
