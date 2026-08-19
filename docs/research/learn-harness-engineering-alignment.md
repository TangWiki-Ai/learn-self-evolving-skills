# 课程交付与前端建设对齐研究报告

> 对比基线：本项目 `e6a0bb90d1520b4b22f8be33b6c80cee2d39eeb5`；参考项目 `a3042405932857f995482366cf2c27cf518605ba`。

## 1. 结论

本项目已经具备一套比参考项目更严谨的“学习实验内核”：10 课 Python 工程覆盖 L1/L2/L3，可重放证据、Gate、Registry、holdout、安全与预算形成完整闭环，并有 1071 个项目测试和 77 个课程测试，fixed/no-key quickstart 也降低了首次运行门槛。真正的短板不是缺少另一套应用，而是课程交付层薄、学习路径不连贯、部分练习与承诺不一致。

建议先修课程真实性，再建前端。前端应采用 `website/` 下的 VitePress 静态课程书站，配少量 Vue islands；继续复用现有 Python CLI 和 HTML 报告，通过 GitHub Pages 发布。不要引入 Next.js、FastAPI、完整 LMS，也不要让浏览器持有 API Key。这样能在不分叉核心架构的前提下，把已有工程证据转化为可读、可搜、可导航、可验证的学习体验。

目标体验应形成一条闭环，而不是把网页和 CLI 做成两套产品：

```text
课程站说明“为什么、做什么”
  → 本地 CLI 运行真实实验
  → learner test 给出形成性反馈
  → L1/L2/L3 解释证据与差异
  → portfolio 汇总学习成果
```

因此，不应把参考项目当成要追平的上限。本项目已经在自动判分、可重放证据、数据隔离和版本治理上更强；需要借的是它的课程包装和学习叙事，再补上它也没有解决好的自动反馈。

## 2. 有证据的对比矩阵

| 维度 | 参考项目 | 本项目 | 判断 |
|---|---|---|---|
| 交付界面 | VitePress 1.6.4、Mermaid、轻量主题、搜索、sidebar、prev/next、GitHub Pages；15 种语言，14 篇讲义、8 个项目页 | 无站点、Actions、搜索、进度、prev/next、截图 | 前端交付明显落后 |
| 内容组织 | 理论讲义与项目分层；中文讲义和项目约 3446 行 | 10 篇课程 README 共 942 行；深度内容主要藏在 specs | 应把规范改写为 learner narrative |
| 实验资产 | React/Electron 知识库作为反复改造的完整样例；含预览、资源库、PDF/Pages | 10 课 Python 工程，L1/L2/L3，CLI、HTML 报告、可重放证据 | 本项目实验内核更强，不必复制其应用栈 |
| 正确性与治理 | 度量偏手工；README/清单存在 13/7 与 14/8 漂移；实际 starter/solution 仅 `project-01..06` | Gate、Registry、holdout、安全、预算；1071 tests、77 lesson tests | 本项目适合建立机器可验收课程目录 |
| 练习真实性 | 有完整项目实验，但清单与目录不完全一致 | Lesson 3 solution 多为一行 re-export；Lesson 7/10 多转发生产函数；后半 starter 使用 `object`、`*args/**kwargs` | 必须先消除“看似实现”的练习 |
| 学习测试 | 项目式 starter/solution 结构 | 逐课 `pytest` 默认测 solution，并断言 starter 抛 `NotImplementedError`；学生完成后原命令会失败 | 当前红转绿路径不成立 |
| 证据呈现 | 视觉预览与页面资源较强 | 无 transition manifest；live/human review 未完成；L7 fixed 为 15/15 对 15/15，无可见提升 | 需展示状态变化，而非只展示最终分数 |

证据入口包括参考项目的 [依赖配置](https://github.com/walkinglabs/learn-harness-engineering/blob/a3042405932857f995482366cf2c27cf518605ba/package.json)、[站点配置](https://github.com/walkinglabs/learn-harness-engineering/blob/a3042405932857f995482366cf2c27cf518605ba/docs/.vitepress/config.mts)、[Pages 工作流](https://github.com/walkinglabs/learn-harness-engineering/blob/a3042405932857f995482366cf2c27cf518605ba/.github/workflows/deploy-pages.yml)、[中文首页](https://github.com/walkinglabs/learn-harness-engineering/blob/a3042405932857f995482366cf2c27cf518605ba/docs/zh/index.md)和 [Project 01 starter](https://github.com/walkinglabs/learn-harness-engineering/blob/a3042405932857f995482366cf2c27cf518605ba/projects/project-01/starter/package.json)，以及本项目的 [README](https://github.com/TangWiki-Ai/learn-self-evolving-skills/blob/e6a0bb90d1520b4b22f8be33b6c80cee2d39eeb5/README.md)、[课程交付规范](https://github.com/TangWiki-Ai/learn-self-evolving-skills/blob/e6a0bb90d1520b4b22f8be33b6c80cee2d39eeb5/docs/specs/09-course-delivery.md)、[Lesson 2 测试](https://github.com/TangWiki-Ai/learn-self-evolving-skills/blob/e6a0bb90d1520b4b22f8be33b6c80cee2d39eeb5/course/ch02-grade-terminal-state/tests/test_baseline.py)和 [Lesson 3 solution](https://github.com/TangWiki-Ai/learn-self-evolving-skills/blob/e6a0bb90d1520b4b22f8be33b6c80cee2d39eeb5/course/ch03-calibrate-judges/solution/llm_judge.py)。

## 3. Copy / Adapt / Do not copy

**Copy**：VitePress 信息架构、本地搜索、sidebar、prev/next、Mermaid、视觉预览、静态资源库和 Pages 自动部署。课程正文稳定后再增加 PDF。

**Adapt**：把参考项目的“讲义 + 项目”改成“概念 + 练习 + 证据 + 复盘”；用机器生成 catalog 防止数量漂移；把 Python 生成的 HTML 报告作为只读 artifact 嵌入或链接；先做中文主线，再按真实需求扩展语言。

**Do not copy**：不要复制 React/Electron 样例、手工维护计数、名义项目页、15 语言规模或偏手工度量。参考知识库只是课程样例，不是 LMS；本项目也不该为“像产品”而复制 LMS 功能。

## 4. 前端方案

可以做，而且现在适合做轻前端。建议目录如下：

```text
course/catalog.json
website/
  package.json
  package-lock.json
  .vitepress/
    config.mts
    theme/
      index.ts
      style.css
      components/
        CourseMap.vue
        LessonMeta.vue
        ArtifactCard.vue
        ProgressTracker.vue
        EvidenceBadge.vue
  index.md
  start/index.md
  course/index.md
  reports/index.md
  evidence/index.md
  troubleshooting/index.md
  public/reports/            # 构建时只复制公开 allowlist
  scripts/sync-course.mts
.github/workflows/deploy-pages.yml
```

课程 README 是正文单一来源。`sync-course.mts` 读取 `course/catalog.json` 与各课 README，在临时目录生成站点页面、sidebar 和 prev/next，不提交第二份手写课文；构建时校验缺页、重复 slug、失效链接与元数据。Python CLI 继续运行实验并生成 HTML/JSON 证据；站点只按 public allowlist 复制报告，拒绝密钥、原始提示、私有样本和任意路径。报告可用受限 `iframe sandbox` 预览，并保留新页面打开入口。`ProgressTracker` 仅存本地浏览器进度，不建立账号系统；交互组件只做可解释的纯前端演示，不请求模型服务。

## 5. 路线图

### P0：先修课程合同

重写 learner tests，让同一条命令在 starter 上红、学生实现后绿，且不因“不再抛异常”失败。把 Lesson 3、7、10 的一行转发改成课程拥有的、类型完整的核心判断 seam；生产实现可以作为最终参考，但 solution 需要解释关键决策，不能只 re-export。收紧后半 starter 类型，移除含糊的 `object` 与 `*args/**kwargs`。新增 transition manifest，明确 before/after、输入、产物和判定；如实标记 live/human review 状态。为 L7 增加能体现策略差异的教学 sandbox，不能再用 15/15 对 15/15 暗示提升。

### P1：4–6 天站点 MVP

第 1 天建立 catalog 与同步脚本；第 2 天完成 VitePress 导航、搜索和主题；第 3 天接入五个组件与 artifact allowlist；第 4 天补 Pages、死链和泄漏扫描；第 5–6 天做移动端、截图、无障碍与试学修正。站点至少包含首页、开始学习、四阶段课程地图、十课页面、报告画廊、证据与边界、排障七类页面。P0 是发布门槛，但 P0 与 P1 可以并行开发。

### P2：教学深度与四个 labs

把 specs 中的原理改写进每课叙事，并加入 Trace/StateDiff、Judge confusion matrix、Gate Playground、Version DAG 四个纯前端 lab。每个 lab 都要关联一项 Python 实验与一份可下载证据，避免互动成为装饰。

## 6. Issue-sized backlog 与验收

1. **修课程测试入口**：学生不改测试即可从红转绿；CI 同时验证 pristine starter 会红、solution 会绿、完成版会绿。
2. **替换转发式练习**：Lesson 3/7/10 各自产生可审查的实现 diff，并通过课程测试。
3. **建立 transition manifest**：每课声明状态迁移、证据路径和判定器；站点能渲染 EvidenceBadge。
4. **统一 catalog**：课程数、页面数、路径、标题、时长只从 `course/catalog.json` 生成；README、导航和构建结果一致。
5. **补齐课程模板**：每课都有目标、时长、输入、任务、验收、证据、复盘、next，构建缺项即失败。
6. **保证首次成功**：全新环境使用 fixed/no-key 路径，学习者在 10 分钟内生成第一份可打开、可解释的证据。
7. **上线站点 MVP**：搜索、sidebar、prev/next、移动端和进度组件可用；Pages workflow 成功。
8. **守住发布边界**：docs build、dead-link scan、secret/leak scan 全绿；public 目录只含 allowlist 文件。
9. **开展目标试学**：邀请 3 名目标学习者完成前两课，记录首次证据时间、卡点和误解；关闭高频阻塞后再宣布发布。

第一批不要同时重写十课。建议交付一个可验证纵向切片：`catalog + learner test runner + 重写后的 Lesson 2 + 站点首页/开始学习/Lesson 2/L1 报告 + Pages preview`。这条切片通过三人试学后，再用同一模板扩展其余课程。

## 7. 风险

最大风险是前端掩盖课程合同缺陷，因此 P0 必须成为发布门槛。第二个风险是 README、catalog、测试和页面再次漂移，必须靠生成与 CI 阻断。第三个风险是公开报告泄漏输入或密钥，必须默认拒绝并使用 allowlist。第四个风险是交互 lab 与真实 Python 行为分叉，应共享固定 fixtures 和预期结果。第五个风险是中国网络下的外部字体和 CDN；站点应使用系统字体或仓库内资产。报告中的通过/失败也不能只靠颜色表达。最后，内容扩写可能变成复制 specs；每段理论都应落到任务、证据和复盘，不以字数代替教学效果。
