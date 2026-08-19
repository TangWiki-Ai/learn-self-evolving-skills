# 首发验证报告

日期：2026-08-19

## 结论

十课的 fixed/offline 路径已经实现并通过自动验证。当前提交不能当作 canonical live
首发：本轮没有可用的 SiliconFlow 凭据，没有重跑 live Gate、live auto-evolve 或 live
final；人工复核也尚未签署。仓库把这些项目保留为明确 deviation，不生成或伪造 live、
`human_reviewed` 或生产数据结论。

正式 clean-room 命令证据必须绑定最终 `HEAD`，因此不嵌入这个会参与 commit hash 的文件。
发布执行器把脱敏 JSON 证据写到仓库外；最终 handoff 记录该文件、commit 和验证结果。

## 已运行的门禁

在完成代码、安全复审和固定产物重建后，实际运行：

```bash
uv sync --all-extras --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
git diff --check
```

结果：

- Ruff format：349 个文件已格式化。
- Ruff check：通过。
- mypy：206 个 source files 通过。
- pytest：1071 passed、2 skipped、0 warnings，用时 689.88 秒。两项 skip 是 live
  测试。
- 十课独立测试：77 passed，逐课为 8、3、10、8、8、8、6、7、9、10。
- `git diff --check`：通过。
- 外部 holdout、课程产物、Registry、portfolio、凭据、本机路径和 hidden-gold 扫描：通过。
  不注入外部 bundle 时，发布验证器只验证 opaque lock 并明确返回 deviation。
- 注入两套 full 数据 bundle、受保护 holdout v3 和 pinned archive 的发布验证器得到
  15 PASS、0 FAIL、5 DEVIATION；正式 clean-room command evidence 要在最终 commit 后生成。

## 数据与 split

- STATE-Bench：固定上游 commit，用于 9 条 creator seed 和独立 holdout。selection 为 6
  题，final 为 12 题。
- ABCD：10,042 段中精确取得 1,070 段 `product_defect`；original/delexed 各
  1,070 条、28,535 turns；train/dev/test 为 863/102/105。
- tau2：只读聚合 1,824 runs 为 114 tasks，每题 16 runs；hard/medium/easy 为
  10/34/70。
- 两个不同临时目录中的 full 数据 bundle 对七项输出逐 byte 相同；浮点置信度在序列化前
  固定为 Decimal 12 位。上游文件的 commit、MIT License、checksum 和 transformation
  manifest 均保留。
- creator、develop、selection、final 在 source ID、semantic group、case ID 和 content
  hash 四个维度互斥。selection/final 不根据当前 Skill 表现挑选。选题使用仓库外的受保护
  semantic mapping 和秘密 HMAC key；两次独立生成逐 byte 相同，完整 bundle 的目录权限为
  `0700`、文件为 `0600`。
- Git 只保存 6/12 个通用 slot、固定上游版本和整体 commitment；逐题请求、source identity、
  fixture、oracle、rubric、eligible membership、semantic mapping、完整 inventory 与选题 key
  都留在仓库外。

selection manifest SHA256：
`6e26436284742b8f35d0915d189a895a4c030475ca56ca0652f372e0c6f02f69`

final manifest SHA256：
`2c97007c383eb617f03610f81c13353ac06d034a0802b0b6f7b21a2a43018b9a`

private inventory SHA256：
`965fead7ed2fb384607d5a6ec341c3c5433f413d0f5a2cd88e5b7d00aea7083b`

holdout commitments SHA256：
`593bd8f6e15e7b090295395e9c44662f2779826acdc026bd4fc05ff51e584157`

外部 bundle tree SHA256：
`6c436d49ea953cb67b2511ca85d2b23e9fa3e84db342f7a009decf452cbc95ef`

## Gate 与 Registry

Gate 按 candidate validation、Static、Trigger、selection、cost、freshness、policy lock 和
terminal decision 的固定顺序运行。修改流程只取得 selection 聚合结果；private pair、逐题
事件、fixture、oracle 和 rubric 不进入公开 decision 或 portfolio。

Registry 保存不可变 Skill、完整 candidate audit bundle 和 hash-chain event。注册事件逐项
承诺 failure evidence、Failure Card、Patch 和 summary；Gate snapshot 必须与注册快照逐 byte
一致。审计会重跑初始 Static Gate、复核嵌套证据、检查未声明文件、credential、checkpoint
event count 和 head hash。

live Registry 要求至少 32 bytes 的 `SES_REGISTRY_CHECKPOINT_HMAC_KEY`。fixed/offline
参考包只标为 `local_untrusted`。HMAC 能检测事件尾部单独删除、伪造 checkpoint 和 append
崩溃后的不一致，但单机可写存储无法阻止攻击者同时回放旧的真实 event log 与当时的真实
HMAC checkpoint；正式 live 部署仍需要外部单调或防回滚 checkpoint backend。

## 两轮自动进化

签入的 fixed/offline 实验完成两轮：

1. Round 1 从 `a19c423b…` 生成 `e19cca2b…`，Gate 接受并 promote。
2. Round 2 从 `e19cca2b…` 生成 `2937178d…`，selection 持平，Gate 拒绝，不改变 accepted
   pointer。

公开 Registry 谱系有 6 个事件：initialized → registered → accepted → promoted →
registered → rejected。当前 accepted Skill 为
`e19cca2b92401b66c62448441773c535d030678f37bb57f45978015f2e76b533`。

循环支持最大轮数、token、费用、连续拒绝、冷却、冻结、收敛、中断恢复和幂等。它在每个
潜在付费步骤前检查剩余额度；未知费用或错币种立即停机。实验根锁、journal intent/receipt、
输出 hash、final protocol/run-set receipt 和独立 consumed checkpoint 防止重复执行或重复记账。

循环停止后只对当前 accepted Skill 运行一次 fixed final 12 题，结果为 10/12。final aggregate
不含逐题结果，且不会进入下一轮或生成 Patch。这个结果来自 `synthetic_offline` fixed
adapter，不是 canonical live final。

portfolio semantic SHA256：
`e17b6531f685d87cc498a5deac98cbea473018ec84a66c24e9a826c9bc06db88`

## 报告与成本

- L1：382,964 bytes。
- L2：52,845 bytes。
- L3：10,859 bytes，包含版本 DAG、拒绝分支、selection 能力/累计成本曲线、final 独立区和
  portfolio allowlist；没有外链或隐藏逐题结果。
- measured canonical：本轮 SiliconFlow 调用 0 次，实际支出 0 CNY。
- fixed：两轮 synthetic/offline 参考记账 0.02460 USD；它不是实际付款。
- estimated：用户随后放宽了原 ¥20 上限；本轮仍只做一条极小 supplemental smoke。
- noncanonical：ChatAnywhere 模型列表检查通过；Claude Haiku 4.5 smoke 返回 HTTP 200 和预期
  `OK.`，使用 11 input + 5 completion tokens。API 响应不返回账单金额；按 ChatAnywhere
  2026-08-19 公开价计算约为 0.00018 CNY。它没有运行 selection/final，也不能充当
  SiliconFlow canonical 证据。

## 未解决项

1. [`human-review-packet.md`](human-review-packet.md) 尚未由用户直接签署。它集中包含 Lesson 3、
   9 条 creator、15 条 develop 和 PRD 首发前 12 项；这是唯一需要明早人工处理的 packet。
2. canonical live Gate、auto-evolve 和 final runner 尚未实现或复测；状态为
   `live_not_rerun`。历史 2026-08-16 smoke 不能替代本轮证据。
3. live Registry 缺少外部单调、防回滚 checkpoint backend。
4. 九组上一课 solution → 下一课 starter 尚无统一 transition manifest；课程测试验证各自入口，
   但不能把目录差异冒充机械继承证明。
5. L3 暂无逐轮 develop quality aggregate；它只展示 fresh rollout provenance、selection aggregate
   和累计成本。
6. ABCD/tau2 的约 131 MB pinned downloads 没有提交到 Git。clean-room 会明确记录该命令
   deviation；两套外部 full bundle 提供重复生成证据。
7. STATE-Bench return 的公开上游池只有 33 个 task；受保护 mapping、eligible membership、
   ranking、split、逐题 identity 和 gold 都不公开，但最终仍从 19 个 eligible semantic group
   中使用 18 个。这个比例使攻击者能针对很小的公开 source universe 泛化调参，因此本次不
   声称强抗污染 secrecy。后续需要扩大 source pool，或增加经过验证的 keyed policy variants。
8. PRD 首发前 12 项仍需用户在集中 packet 中逐项确认。

仓库不创建 release tag，也不发布 GitHub Release。
