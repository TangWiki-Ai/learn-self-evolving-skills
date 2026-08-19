---
title: 排错指南
description: 解决安装、命令、输出目录和课程测试中的常见问题。
---

# 排错指南

先确认你在仓库根目录运行命令。大多数问题来自 Python 版本、依赖没有同步，或者重复使用已有输出目录。

## `uv` 找不到 Python 3.11+

检查当前环境：

```bash
uv run python --version
```

如果版本不符合要求，先按 [`uv` 的 Python 安装指南](https://docs.astral.sh/uv/guides/install-python/) 安装合适版本，再重新同步依赖。

## `ses` 命令不存在

在仓库根目录重新安装项目依赖：

```bash
uv sync --all-extras --locked
uv run ses --help
```

不要跳过 `uv run`。它确保命令使用当前项目环境。

## 输出目录已经存在

课程命令通常拒绝覆盖已有产物。这能保护你的实验记录。

换一个新目录：

```bash
uv run ses run-case --output-root .ses/my-next-run --json
```

如果你要继续一项支持恢复的实验，请使用对应课程文档明确提供的恢复方式。不要直接覆盖旧目录。

## 课程测试失败

先只运行当前课程测试，缩小问题范围：

```bash
uv run pytest course/ch01-see-the-difference/tests
```

然后检查：

1. 你是否只修改了本课 starter。
2. 测试当前指向 starter 还是 solution。
3. 失败信息描述的是未实现缺口，还是环境安装问题。

::: warning 学习者测试仍在完善
当前测试主要维护课程基线。部分课程会同时确认 starter 保留预期缺口、solution 保持通过。请阅读本课 README，确认怎样把测试目标切换到你的实现。
:::

## 固定结果与预期不同

先确认你运行的是 `--mode fixed`，并使用了新的输出目录。固定路径应该可复现，但你修改 starter、课程数据或运行参数后，结果自然会变化。

不要用站点摘要替代你的 fresh run。摘要只帮你理解协议。

## 自动进化中途停止

不要立刻重跑并覆盖目录。先保留现有记录，检查输出中最后一个完整步骤。

自动流程只会安全复用已经完整落盘的步骤。如果系统只写下执行意图，却没有完整产物，它会要求人工判断，避免重复执行不确定操作。

## 我可以直接运行联网路径吗

当前课程承诺的是 fixed/offline 学习路径。联网端到端路径和独立人工复核尚未完成。

先完成离线课程。不要把固定参考改名成真实运行，也不要用它宣称实际效果。

## 仍然无法解决

提交 Issue 时请包含：

- 你运行的命令。
- Python 与 `uv` 版本。
- 哪一课、哪一步失败。
- 删除凭据和本机绝对路径后的最小错误信息。
- 你已经尝试过哪些检查。

[前往 GitHub Issues](https://github.com/TangWiki-Ai/learn-self-evolving-skills/issues) · [返回起步指南](/start/)
