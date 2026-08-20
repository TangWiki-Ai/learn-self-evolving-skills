# 无 Key reference 边界

仓库提供两类无 Key 参考材料：

1. [`fixtures/creator-projections/`](fixtures/creator-projections/) 中的八份课程原创、已脱敏
   Creator projection；
2. packaged [`shopping-assistant`](../../src/ses/skills/resources/shopping_assistant/SKILL.md)
   Skill，它只标记为 `reference_fallback`，用于阅读通用工作流和兼容安装路径。

这些材料不是 Creator seed、gold、默认 accepted，也不是学习完成证据。你必须从自己的
fixed run 产生 create、Static、10/10 Trigger、fresh pair、Failure Card、Patch、GateDecision、
Registry、automation、final、L3、portfolio 和 package receipts，并保留 accepted install 的
校验输出。安装不会反向写入 `CapstoneIndex`。

clean-room report 会逐条记录 fixed 目标 CLI 的 passed/failed 状态。维护者以后可以加入脱敏的 fixed 参考
Trace、L2、Failure Card、rejected GateDecision、L3 和 portfolio，但每份都必须标记
`fixed_offline_reference`，只含公开投影，并继续设置 `completion_evidence=false`。
