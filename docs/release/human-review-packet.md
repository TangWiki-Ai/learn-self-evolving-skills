# 首发集中人工复核包（待签署模板）

这份 packet 集中记录机器无法代替你的判断。所有复选框默认留空。AI、自动测试和固定 fixture 不能替你确认这些项目，也不能把已有的委托记录当作你的决定。

填写信息：

- 审核人：________________
- 审核日期（UTC）：________________
- 审核 commit：________________
- 审核环境与 Provider：________________

“审核 commit”必须是当前仓库历史中包含待审核 `develop-manifest.json` 与 v0 `skill-manifest.json` 的完整 commit SHA。激活脚本会读取该 commit 中的两份文件，并拒绝审核后发生的任何替换。

## A. v0 Skill 的 9 条来源链复核

主入口是 [`review-packet.json`](../../data/skill-v0/creator/review-packet.json) 和 [`seed-manifest.json`](../../data/skill-v0/creator/seed-manifest.json)。`creator/` 是历史证据路径，不代表当前产品会自动生成 Skill。你要直接核对 pinned source、工具回放、StateDiff、确定性 State Judge、模型证据和公开 projection。文件中任何“委托给 AI”的旧决定都不能替代本表。

| Seed | Source | Replay | State | Model evidence | Projection | 你的决定/原因 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| creator-seed-001 | [ ] | [ ] | [ ] | [ ] | [ ] | |
| creator-seed-002 | [ ] | [ ] | [ ] | [ ] | [ ] | |
| creator-seed-003 | [ ] | [ ] | [ ] | [ ] | [ ] | |
| creator-seed-004 | [ ] | [ ] | [ ] | [ ] | [ ] | |
| creator-seed-005 | [ ] | [ ] | [ ] | [ ] | [ ] | |
| creator-seed-006 | [ ] | [ ] | [ ] | [ ] | [ ] | |
| creator-seed-007 | [ ] | [ ] | [ ] | [ ] | [ ] | |
| creator-seed-008 | [ ] | [ ] | [ ] | [ ] | [ ] | |
| creator-seed-009 | [ ] | [ ] | [ ] | [ ] | [ ] | |

- [ ] 我确认每条来源来自固定 STATE-Bench commit，且 hash 与 manifest 一致。
- [ ] 我确认公开 projection 不包含留出答案、Gold、凭据或完整私有链。
- [ ] 我记录了每条不接受的具体原因，没有为了凑足 9 条而降低标准。

## B. 15 条 develop 用例复核

查看 [`review-packet.json`](../../data/testset/ticket07/generated/review-packet.json) 和 [`develop-manifest.json`](../../data/testset/ticket07/generated/develop-manifest.json)。每题都要核对 source 意图、确定性 oracle、标准操作 replay、故意正确/错误/证据不足三种 Judge 结果。当前 manifest 只允许 fixed/offline；它不能替代你的决定。完成 A、B 并签署下面的资产激活决定后，才能将这些输入用于 release-candidate live 验证。

| Develop case | Source/rubric | Oracle/replay | Judge P/F/N | 你的决定/原因 |
| --- | --- | --- | --- | --- |
| develop-return-01e57efeb7f9b2bb1179 | [ ] | [ ] | [ ] | |
| develop-return-06308f2fc2391e1c0062 | [ ] | [ ] | [ ] | |
| develop-return-0fd8a7f97670f18f9266 | [ ] | [ ] | [ ] | |
| develop-return-13a986c3046656aafbcf | [ ] | [ ] | [ ] | |
| develop-return-1eea7b78b3fbcda9c5e8 | [ ] | [ ] | [ ] | |
| develop-return-29d1c939afee990529ca | [ ] | [ ] | [ ] | |
| develop-return-35f540ffc55b4bb29ed8 | [ ] | [ ] | [ ] | |
| develop-return-5c40e36fbbf915be8454 | [ ] | [ ] | [ ] | |
| develop-return-65a595515e9a2273cdab | [ ] | [ ] | [ ] | |
| develop-return-80cc1e589981ec8f5586 | [ ] | [ ] | [ ] | |
| develop-return-b85ca3b6062ca8686d8f | [ ] | [ ] | [ ] | |
| develop-return-c009983da0ce8791f3b4 | [ ] | [ ] | [ ] | |
| develop-return-c24b05408eda7561ffbd | [ ] | [ ] | [ ] | |
| develop-return-dadfff3f9719005a2c16 | [ ] | [ ] | [ ] | |
| develop-return-db669964b6006f41abae | [ ] | [ ] | [ ] | |

- [ ] 我确认 15 题只能进入 develop，没有写入锁定的 selection/final。
- [ ] 我确认模型只提出语言与 rubric，金额和终态来自确定性 policy。
- [ ] 我记录了所有不接受题目及原因。

## C. 资产激活决定

这一步只批准 v0 Skill 和 15 条 develop 用例进入 release-candidate live 验证，不等于批准发布。脚本会绑定本表中的审核 commit，更新两个 manifest，并重算 catalog 的 `data_version`。

- [ ] 允许激活：A、B 的必需项均已核对，没有未处理的拒绝项。
- [ ] 暂不激活：我已在 A 或 B 中写明原因。

资产复核签名：________________　时间（UTC）：________________

签署“允许激活”后运行：

```bash
uv run python scripts/activate_reviewed_assets.py --confirm-signed-asset-review
```

脚本会拒绝未填字段、未勾选项、不存在或不属于当前历史的审核 commit、审核后被替换的 manifest、无签名或同时选择“暂不激活”的 packet。激活后再运行两家 Provider 的 live 路径，填写下一节。

`tests/engines/test_live.py` 是代表性组件 smoke。它可以用 pending fixture 检查 Model、Skill、Shop 和 Judge 是否连通，但不能替代 A、B，也不能单独作为发布验收或模型成绩。

## D. 当前 PRD 首发前 8 项

| ID | 事项 | 证据路径/运行 ID | 你的结论与 deviation |
| --- | --- | --- | --- |
| PRD-01 | fixed 8 步路径在干净 workspace 全链路通过 | | |
| PRD-02 | 资产激活后，SiliconFlow live Journey 验证模型、MCP、Skill 和 Judge | | |
| PRD-03 | 资产激活后，ChatAnywhere live Journey 验证锁定 Claude 模型、MCP、Skill 和 Judge | | |
| PRD-04 | Provider 选择、恢复、模型锁和凭据隔离 fail closed | | |
| PRD-05 | ChatAnywhere 费用 unavailable 贯穿 Engine、runner、报告和 dashboard | | |
| PRD-06 | dashboard 的只读、路径和 symlink 边界通过 | | |
| PRD-07 | `git clone` 后核对 instructor Skill 与 8 个步骤 playbook；clean-wheel 核对 `ses journey --help` | | |
| PRD-08 | Ruff、mypy、pytest 与文档命令检查全绿 | | |

## E. 最终决定

- [ ] 允许发布：所有必需项有直接证据，没有未解释的 fail/deviation。
- [ ] 暂不发布：我在上表写明 blocker 和下一步。

签名：________________　时间（UTC）：________________

只有完成资产激活、两家 Provider 的 live 验证、8 项首发检查，并在这里选择“允许发布”且签名后，才能发布。资产激活前，live Journey 会 fail closed；fixed CI 仍可运行。
