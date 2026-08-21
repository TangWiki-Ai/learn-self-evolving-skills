# `learn-harness-engineering` 设计与语言审计

> 调研日期：2026-08-21。参考版本：[`e205c6f`](https://github.com/walkinglabs/learn-harness-engineering/tree/e205c6f1d3cd6455ac4db9ecf6e3838a6dcda513)。只采用仓库 README、站点源码、样式和部署配置。

## 核心判断

这个项目真正干净的是课程站，不是 689 行的仓库 README。课程站把信息分层：README 负责说明仓库是什么；首页负责分流；讲义、项目和资料库各自承接细节。2026-04-02 的重构提交甚至把中文首页从 177 行减到 82 行，同时明确要匹配 Anthropic/Claude 风格。简洁首先来自删减和分流，其次才是 CSS。[重构提交](https://github.com/walkinglabs/learn-harness-engineering/commit/a7f05ab505f23db7778e6e252856723f6af2b49d)

## 定位、信息架构与语言

项目用一句话把自己定义为“让 AI coding agent 可靠工作的项目制课程”，核心对象是已经使用 coding agent 的工程师、研究者/构建者和技术负责人；它明确排除零代码入门和只关心 prompt 的读者。[README 定位](https://github.com/walkinglabs/learn-harness-engineering/blob/e205c6f1d3cd6455ac4db9ecf6e3838a6dcda513/README.md#L19-L31) · [受众](https://github.com/walkinglabs/learn-harness-engineering/blob/e205c6f1d3cd6455ac4db9ecf6e3838a6dcda513/README.md#L528-L561)

首页不用营销型大 Hero。它先给标题和两段定义，再用四张卡分到“讲义 / 项目 / 资料库 / 前沿拆解”，随后只保留一张机制图、五项学习结果和三个下一步。[中文首页](https://github.com/walkinglabs/learn-harness-engineering/blob/e205c6f1d3cd6455ac4db9ecf6e3838a6dcda513/docs/zh/index.md#L1-L77) 页面外壳固定为顶部导航、左侧章节、中央正文、右侧二三级目录；搜索、上一篇/下一篇、语言和主题切换由 VitePress 提供。[站点配置](https://github.com/walkinglabs/learn-harness-engineering/blob/e205c6f1d3cd6455ac4db9ecf6e3838a6dcda513/docs/.vitepress/config.mts#L830-L866)

有效文案使用事实和动作：标题直接说问题，项目页直接写“你要做什么、具体步骤、怎么衡量、要交什么”。它常用“不是 X，而是 Y”划边界，也直接用“你”。弱点同样明显：README 顶部塞了 15 个语言徽章和两轮更新公告；“最前沿”“残酷事实”等句子也有营销味。因此应借站点语言，不应照抄 README 的膨胀。[README 结构](https://github.com/walkinglabs/learn-harness-engineering/blob/e205c6f1d3cd6455ac4db9ecf6e3838a6dcda513/README.md#L1-L141)

## 视觉 token

- 字体：Newsreader 用于大标题，Inter 用于正文，JetBrains Mono 用于代码。
- 颜色：暖白 `#FAF9F5`、浅米灰 `#F4F3EE`、墨黑 `#1A1A1A`、陶土橙 `#D95C41`；分隔线只用 8% 黑。
- 尺度：页面最大宽度 1376px，侧栏 296px；正文 16px / 1.75，H1 3.5rem，H2 2rem。
- 组件：1px 边框、8–12px 圆角、轻背景；只有主 CTA 使用黑色胶囊按钮。
- 动效：只做 0.2–0.3 秒颜色变化和 1–2px 上移；没有渐变、滚动入场或装饰动画。[完整样式](https://github.com/walkinglabs/learn-harness-engineering/blob/e205c6f1d3cd6455ac4db9ecf6e3838a6dcda513/docs/.vitepress/theme/style.css#L1-L200)

## 五条可迁移原则

1. README 首屏只回答：这是什么、给谁用、解决什么、第一条命令是什么。
2. 让 README、教学内容和 dashboard 各守一个职责；dashboard 只显示当前状态、下一步和证据。
3. 用“问题 → 操作 → 验收 → 产物”命名区块，删除“经历”“蜕变”“带走”等空泛承诺。
4. 只在真正需要选择时用卡片；其余内容用标题、短段落、列表和细分隔线。
5. 建立自己的暖中性色、正文宽度和间距 token，以排版层级承担主要视觉表达。

## 三条不可照抄

1. 不复制星形标志、Newsreader + 陶土橙的整套品牌外观；应保留原则，建立本项目辨识度。
2. 不复制长 README、徽章墙、多语言配置和更新公告；参考项目已经出现“README 写 8 个项目、中文项目页写 7 个、源码只有 6 组 starter/solution”的漂移。
3. 不把 VitePress 三栏阅读站硬套到本地动态 dashboard，也不照抄 `ignoreDeadLinks: true`、远程 Google Fonts 和按链接 URL 匹配 CTA 的脆弱实现。[构建配置](https://github.com/walkinglabs/learn-harness-engineering/blob/e205c6f1d3cd6455ac4db9ecf6e3838a6dcda513/docs/.vitepress/config.mts#L886-L923) · [Pages 部署](https://github.com/walkinglabs/learn-harness-engineering/blob/e205c6f1d3cd6455ac4db9ecf6e3838a6dcda513/.github/workflows/deploy-pages.yml#L1-L59)
