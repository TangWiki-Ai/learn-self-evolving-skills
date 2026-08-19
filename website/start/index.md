---
title: 5 分钟开始
description: 在固定离线路径中运行第一个 case 和两轮自动进化。
---

# 5 分钟开始

先跑通，再读细节。课程运行使用固定数据和本地运行时，不读取 API Key，也不访问模型服务。首次克隆仓库和安装依赖仍需要联网。

## 你需要准备什么

- Python 3.11 或更高版本
- Git
- [`uv`](https://docs.astral.sh/uv/)
- 一个可以运行终端命令的环境

## 1. 获取代码

```bash
git clone https://github.com/TangWiki-Ai/learn-self-evolving-skills.git
cd learn-self-evolving-skills
uv sync --all-extras --locked
```

确认课程入口已经安装：

```bash
uv run ses --help
```

## 2. 运行一个固定 case

```bash
uv run ses run-case --output-root .ses/site-quickstart --json
```

这条命令会创建一份 fresh 运行记录。先观察三件事：

1. Agent 的回复与工具动作分别保存。
2. 执行前后的环境状态可以比较。
3. Judge 根据状态和规则给出结论，而不是只读最后一句话。

## 3. 运行两轮自动进化

```bash
uv run ses auto-evolve --mode fixed --output-root .ses/site-auto-evolve --json
```

固定场景会演示两种结果：

- 第一轮候选通过 Gate，并成为当前版本。
- 第二轮候选没有改善，因此被拒绝。

重点不是记住结果，而是观察修改流程为什么不能自行宣布成功。

## 4. 建立你的学习节奏

每课都按同一节奏推进：

1. 先读“困惑”，确认这一课要解决什么判断问题。
2. 查看 starter，只实现本课留下的缺口。
3. 阅读本课的验证说明；暂时不要把维护者测试当作学习者验收。
4. 对照 solution 的外部行为，不要直接复制代码。
5. 阅读参考结果，写下它能证明什么、不能证明什么。

::: warning 当前测试边界
仓库现有测试主要用于维护课程基线。部分课程还没有独立的学习者测试入口。开始练习前，请先阅读本课 README 中的测试说明，确认测试目标。
:::

## 完成检查

如果你已经做到下面三点，就可以进入第 1 课：

- `uv run ses --help` 能正常显示命令。
- 单 case 命令生成了新的输出目录。
- 自动进化命令展示了一次接受和一次拒绝。

[查看十课学习路径](/course/) · [遇到问题？查看排错指南](/troubleshooting/)
