---
layout: home
title: Learn Self-Evolving Skills

hero:
  name: Learn Self-Evolving Skills
  text: 亲手构建可评测、可进化、可回滚的 Agent Skill
  tagline: 从一次可复现的失败出发，用十课搭出评测、证据、Gate、Registry 和有界自动进化链路。
  actions:
    - theme: brand
      text: 开始学习
      link: /start/
    - theme: alt
      text: 查看十课路径
      link: /course/
    - theme: alt
      text: 阅读报告
      link: /reports/

features:
  - title: 先证明，再修改
    details: 你先用 Trace、终态和 Judge 建立基线，再根据失败证据生成候选修改。
  - title: 修改者不能当裁判
    details: 独立 Gate 决定候选是否通过。平局、退化和证据不足都会被拒绝。
  - title: 每一步都能追溯
    details: Registry 保留接受、拒绝、晋升和回滚历史，作品集只展示允许公开的证据。
---

<ProgressSummary />

## Skill 看起来更完整。它真的变好了吗？

一次成功对话不够。

Agent 可能只修复了刚看过的样例，也可能说得更漂亮，却没有完成业务操作。更糟的是，一次修改还可能破坏原本能完成的任务。

这门课让你亲手建立判断标准。你会重放同一批任务，检查系统终态，校准 Judge，隔离留出集，把每处修改连回失败证据，并在退化时回滚。

```text
创建 → 评测 → 诊断 → 修改 → 门控 → 版本治理 → 自动循环 → 作品集
```

## 你会构建什么

<CourseMap />

课程使用一个可执行的电商退货场景贯穿十课。完成课程后，你不只会得到一个 Skill，还会得到一套回答这些问题的系统：

- Agent 做了什么？
- 环境真的发生变化了吗？
- Judge 的结论可靠吗？
- 候选修改解决了哪条失败证据？
- 新版本为什么被接受或拒绝？
- 自动循环何时必须停止？

## 三层报告，各自回答一个问题

<ReportCard
  level="L1"
  title="一次运行发生了什么"
  question="Agent 的动作、终态和判分如何对应？"
  summary="从批量基线回到单次运行，检查 Trace、状态变化和 Judge 证据。"
  :related-lessons="[2, 4, 6]"
/>

<ReportCard
  level="L2"
  title="安装 Skill 前后有什么变化"
  question="同一批任务的配对结果改善、持平还是退化？"
  summary="固定参考结果没有改善。两侧全部通过，课程没有制造虚假的提升。"
  :related-lessons="[7]"
/>

<ReportCard
  level="L3"
  title="多个版本怎样演进"
  question="哪些候选被接受或拒绝，当前版本从哪里来？"
  summary="沿着版本谱系查看两轮自动进化、保守门控和最终汇总。"
  :related-lessons="[8, 9, 10]"
/>

[阅读清洗后的报告摘要](/reports/) · [学习怎样检查证据](/evidence/)

## 你适合这门课吗

如果你会使用 Python、终端、Git 和测试工具，写过简单 Agent，并理解工具调用或 MCP 的基本概念，这门课适合你。

如果你只想复制提示词、不准备运行实验，或者需要一个已经托管好的成品服务，这门课不适合你。

## 从离线路径开始

第一次课程运行不需要 API Key，也不会访问模型服务。首次克隆仓库和安装依赖仍需要联网。你可以先用固定场景理解完整协议，再决定是否继续运行自己的实验。

[进入 5 分钟起步指南](/start/)
