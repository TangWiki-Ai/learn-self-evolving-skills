# 课程交付 Spec

## Problem Statement

一个功能完整的自进化框架不自动等于一门可学的课程。学生需要在每一步亲手实现关键判断，又不能被环境、数据下载和安全隔离拖住；每课必须从稳定 starter 开始、通过测试获得反馈，并产出一个能和前一状态比较的数字。如果课程材料、代码版本和参考结果不同步，学生会在错误基础上继续，最终作品也无法复现。

## Solution

把系统能力编排成 6 部分 10 课。每课包含概念讲义、starter、学生实现模块、公开测试、solution、一个对照产物、拓展阅读和预算说明。上一课 solution 必须等于下一课 starter。环境仿真与安全隔离由课程提供；测试集判断、Trace、Judge、Evaluator、Skill 创建验证、进化、Gate 和自动循环由学生实现。仓库提供锁版本数据、小型参考 run 和兜底 Skill，让无 Key 或生成效果差的学习者仍能理解课程。

## User Stories

1. 作为具备 Python 和 Agent 基础的学习者，我想明确前置要求，以便判断自己是否适合课程。
2. 作为学习者，我想从可运行 starter 开始，以便只关注本课新增机制。
3. 作为学习者，我想先看到困惑和失败现象，再学习方法，以便理解为什么需要这个模块。
4. 作为学习者，我想亲手补完核心判断逻辑，以便真正掌握评测和进化系统。
5. 作为学习者，我想运行本课公开测试，以便在进入下一课前发现错误。
6. 作为学习者，我想比较自己的结果和 solution 行为，以便理解差异而不是直接复制代码。
7. 作为学习者，我想每课得到一个 before/after 或 with/without 产物，以便把学习过程变成实验记录。
8. 作为学习者，我想看到本课预计和实测费用，以便控制总预算。
9. 作为学习者，我想在生成 demo Skill 失败时使用参考 Skill，以便继续后续课程。
10. 作为无 Key 的学习者，我想阅读参考 Trace 和报告，以便先学习机制再决定是否付费运行。
11. 作为学习者，我想知道哪些代码是脚手架、哪些必须自己写，以便不绕过课程目标。
12. 作为学习者，我想按课程命令导出作品集，以便展示完整系统而不只展示一份 Skill。
13. 作为课程作者，我想让每课 solution 成为下一课 starter，以便代码与讲义不会漂移。
14. 作为课程作者，我想把拓展阅读映射到具体段落和问题，以便阅读服务当前机制。
15. 作为课程作者，我想锁定数据、模型和协议，以便参考数字可解释。
16. 作为课程作者，我想清楚标记 benchmark 与角色扮演数据，以便不夸大数据来源。
17. 作为测试作者，我想验证 starter 在正确位置失败、solution 全部通过，以便练习没有被意外补完或破坏。
18. 作为评审者，我想从最终 portfolio 回到每课实验，以便确认学习者真的完成了链路。
19. 作为维护者，我想独立更新系统模块 spec 和课程映射，以便架构演进不打乱教学顺序。
20. 作为未来维护者，我想增加 Provider 说明或适配器，以便课程不永远绑定一个服务商。

## Implementation Decisions

- 目标学习者已经写过简单 Agent，理解 function calling 或 Agent 框架，了解 MCP 基本概念，并能读写 Pydantic、类型标注和 pytest。
- 课程代码使用 Python 3.11+、Pydantic v2 和 pytest，不引入重型 Agent 框架。
- 每课固定结构为：困惑、方法、业界做法、关键 insight、starter、实现任务、测试、对照产物、拓展阅读、预算。
- 每课只要求学生实现与学习目标直接相关的判断逻辑。脚手架提供电商数据、shop 环境、MCP server、runtime 隔离、Simulator、安全 Creator Adapter 和 CLI 参数解析。
- 学生实现 Trace、State/Rule/LLM/Agent Judge、Evaluator、Runner、Report、测试集清洗与生成、Static Gate、Trigger Evaluator、Evolution、Gate、Registry 和 Auto-Evolve。
- 上一课 solution 的源码和数据状态必须机械生成或验证为下一课 starter，不能靠人工复制维护两份。
- Starter 包含本课未实现点、公开接口、测试和最少上下文；Solution 只补本课目标，不提前泄漏后续课。
- 每课测试覆盖外部行为，并提供失败信息指导学生定位概念问题，但不直接给出完整实现。
- 课程与模块映射如下：

| 课 | 核心模块 | 学生交付 | 对照产物 |
| --- | --- | --- | --- |
| 1 | Foundation、Shop、Skill Creation | doctor 接入和 demo Skill 使用 | 同 case 无 Skill/有 Skill 两段 fresh 对话 |
| 2 | Shop、Evaluation | Trace、State Judge、Rule Judge、expect | develop 6 题 baseline state pass 率 |
| 3 | Evaluation | LLM Judge、evidence extractor、Agent Judge | 两类 Judge 与人工标签的一致性 |
| 4 | Runtime、Simulation/Runner/Reporting | Evaluator、Runner、L1 Report | baseline 成功率、成本、耗时和方差 |
| 5 | Testset Pipeline | ABCD 清洗聚类、tau2 去重分层 | 1,070 段到候选清单的漏斗 |
| 6 | Shop、Evaluation、Testset Pipeline | 变体、重放、校准和 develop 入库 | 新题合格率与扩容后 baseline |
| 7 | Skill Creation、Runner/Reporting | v0、Static Gate、Trigger Eval、L2 Report | trigger P/R 与 v0/baseline 配对表 |
| 8 | Evolution | Failure Card 和结构化 Patch | 候选补丁及逐条证据链 |
| 9 | Evolution、Registry、Runner | Selection Gate、promote/reject/rollback | v0/v1 GateDecision 与谱系 |
| 10 | Automation、Reporting | Auto-Evolve、final、Portfolio | 进化曲线与 final 12 题报告 |

- 第一课允许生成结果波动，课程提供参考 Skill 兜底并在报告中标记。兜底不能替代第七课的 v0 生成任务。
- 第五课统一使用 benchmark 轨迹或真人角色扮演语料，避免把数据描述成生产日志。
- 第六课只允许合格新题进入 develop。selection 和 final 在课程开始时锁定，后续课程不展示逐题反馈。
- 第十课必须在预算内完成至少两轮完整候选流程，并复现至少一次接受和一次拒绝或回滚。
- 每次 baseline、v0、candidate 和 final 都产生 fresh trace。参考 run 只供阅读，不能代替学习者结课运行。
- 课程总预算目标不超过人民币 50 元。每课先给估算，课程发布前用小样本实测校准并注明价格日期。
- 拓展阅读包括论文、Anthropic 文档、skill-up 和其他机制事实库。每条阅读必须指定阅读范围和要回答的问题。
- 讲义和用户文档使用中文，代码标识、schema 字段和代码注释使用英文，避免同一 API 出现双语名称。
- 作品集包含系统和实验证据，不把“Skill 变好”写成无条件结论。所有数字必须注明数据 split、协议和模型版本。
- 多 Provider 作为 roadmap 文字说明。首版课程只教授薄 Engine 边界和硅基流动主路径，不增加额外 Provider 练习。

## Testing Decisions

- 每课有独立测试入口，先验证 starter 的预期失败，再验证 solution 全部通过。
- 链式一致性测试逐课比较上一课 solution 与下一课 starter，排除本课预留缺口后其余内容必须一致。
- 课程 smoke 测试从文档中的命令运行最短成功路径，避免讲义命令和 CLI 漂移。
- 默认课程测试使用 fake engine 和固定小数据，不访问网络。标记清楚的 live exercises 才读取用户凭据。
- 对照产物测试验证数据来源和协议兼容，而不是锁定某个分数。参考数字只在同模型锁和数据版本下比较。
- 课程内容测试检查每课都有学习目标、给定/学生边界、实现任务、测试、对照指标、预算和阅读。
- 防泄漏测试扫描所有 starter、solution、讲义、报告和可安装 Skill，确保没有 final gold、参考答案、密钥或本机路径。
- 数据诚实性检查扫描公开话术，阻止“真实生产日志”等不符合来源的表述。
- clean-room release test 在新环境按 10 课顺序执行，记录依赖安装、命令、测试、实测费用、时间和人工步骤。
- 无 Key 路径测试验证参考 run、兜底 Skill 和报告可以离线阅读，且不会伪装为当前实验。

## Out of Scope

- 面向完全没有 Python、Agent 或 MCP 基础的入门教学。
- 替学生实现课程指定的核心判断模块。
- 用视频平台、学习管理系统或营销网站替代可运行仓库。
- 承诺不同日期、模型或 Provider 得到与参考 run 相同的精确分数。
- 首版提供多语言讲义、Windows 全支持或多个业务领域。
- 把拓展论文复述成课程主体，或要求学生复现论文全部算法。

## Further Notes

- 开发 tickets 按可验证纵向切片排序，可能与学生看到的课程序号不同；课程交付层负责最终教学顺序。
- 课程发布前应让不同水平的三名模拟学习者完成第一课，确认无 Skill/有 Skill 差异可观察，同时保留参考 Skill 兜底。
- 所有目标数字都必须在实现后实测。PRD 中的预算和一致率是待验证假设，不是既成事实。
