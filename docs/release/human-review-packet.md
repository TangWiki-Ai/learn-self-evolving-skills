# 首发集中人工复核包（待签署模板）

这份 packet 集中记录机器无法代替你的判断。所有复选框默认留空。AI、自动测试和固定 fixture 不能替你确认这些项目，也不能把已有的委托记录当作你的决定。

填写信息：

- 审核人：________________
- 审核日期（UTC）：________________
- 审核 commit：________________
- 审核环境与 Provider：________________

## A. Judge 标签复核

你需要查看 [`judge-model-calibration.json`](../../data/testset/ticket07/generated/private/judge-model-calibration.json) 及 [`calibration.json`](../../tests/fixtures/judges/calibration.json)，逐题核对课程作者给出的目标标签，再决定是否接受。固定响应只证明解析协议可重复，不证明标签已经由人确认。

| Case | 课程作者当前标签 | 你的标签 | 证据充分 | 备注 |
| --- | --- | --- | --- | --- |
| cal-001 | pass | ________ | [ ] | |
| cal-002 | fail | ________ | [ ] | |
| cal-003 | not_evaluated | ________ | [ ] | |
| cal-004 | pass | ________ | [ ] | |

- [ ] 我检查了四题的 rubric、StateDiff、工具时间线、金额对账和关键消息。
- [ ] 我检查了 LLM Judge 与 Agent Judge 的三处分歧，没有根据当前结果反向改标签。
- [ ] 我确认材料只把该实验称为课程作者 fixed/offline 标签，直到我另行签署。

## B. Creator 9 条来源链复核

主入口是 [`review-packet.json`](../../data/skill-v0/creator/review-packet.json) 和 [`seed-manifest.json`](../../data/skill-v0/creator/seed-manifest.json)。你要直接核对 pinned source、工具回放、StateDiff、确定性 State Judge、模型证据和公开 projection。文件中任何“委托给 AI”的旧决定都不能替代本表。

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
- [ ] 我确认 Creator 只接收安全 projection，不接收 selection/final、Gold、凭据或完整私有链。
- [ ] 我记录了每条不接受的具体原因，没有为了凑足 9 条而降低标准。

## C. Ticket 07 develop 题复核

查看 [`review-packet.json`](../../data/testset/ticket07/generated/review-packet.json) 和 [`develop-manifest.json`](../../data/testset/ticket07/generated/develop-manifest.json)。每题都要核对 source 意图、确定性 oracle、标准操作 replay、故意正确/错误/证据不足三种 Judge 结果。课程 attestation 只绑定 fixed/offline 的纳入或排除与当前 evidence hash，状态全部待你确认；它不是人工决定，也不能用于 live 或 release acceptance。

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

## D. PRD 首发前 12 项

| ID | 事项 | 证据路径/运行 ID | 你的结论与 deviation |
| --- | --- | --- | --- |
| PRD-01 | SiliconFlow + Claude Code headless：stream-json、Skill discovery、配置隔离 | | |
| PRD-02 | DeepSeek tool calling 30 case 抽样与失败率 | | |
| PRD-03 | 每课 3 case 的 token/费用实测和价格日期 | | |
| PRD-04 | 三种学习者水平的 Lesson 1 生成与肉眼差异 | | |
| PRD-05 | Creator 9 条三重复核 | 见 B | |
| PRD-06 | Lesson 3 两类 Judge 与人工标签一致率 | 见 A | |
| PRD-07 | 变体 oracle 标准操作 replay 100% 一致 | | |
| PRD-08 | ABCD/tau2 切片脚本、来源、License、checksum | | |
| PRD-09 | Trigger 20 prompts 交叉校验 | | |
| PRD-10 | Portfolio 模式同配置三次方差与 Gate 阈值依据 | | |
| PRD-11 | L1/L2/L3 单文件打开与小于 2 MB | | |
| PRD-12 | 每课拓展阅读范围和问题映射 | | |

## E. 最终决定

- [ ] 允许发布：所有必需项有直接证据，没有未解释的 fail/deviation。
- [ ] 暂不发布：我在上表写明 blocker 和下一步。

签名：________________　时间（UTC）：________________
