# Skill 检查 Spec

## 目标

评测只能安装被测 Skill 的运行时内容。订单标识、固定答案、评测数据或未知工具进入 Skill 后，会让结果失真。

## 当前实现

- Skill artifact 由 `SKILL.md`、可选 `references/` 和 `skill-manifest.json` 组成。
- manifest 列出每个运行文件及 SHA256；验证器拒绝路径穿越、symlink 和 hash 不一致。
- WorkspaceFactory 只把 manifest 声明的 `SKILL.md` 和 `references/` 写入隔离 case workspace 的 Claude 原生 Skill 位置。仓库根目录的 instructor Skill 不进入被测 workspace。
- Static Gate 在运行候选前检查 front matter、工具白名单、危险指令、case/订单标识、固定金额答案、评测内容和文件大小。
- Gate 输出逐条结构化结果。失败候选保留证据，但不能进入目标回放或全量回归。
- Skill identity 来自全部 manifest 文件的规范化内容 hash，而不是只 hash 主文档。

## 测试

- manifest 和 workspace 测试验证精确文件清单、canonical hash、symlink 和路径边界。
- Static Gate 使用表驱动用例覆盖合法 Skill、未知工具、固定答案、标识泄漏、危险指令和长度边界。
- workspace 测试确认被测 Agent 看不到项目源码、gold、其他 case、个人 Skill 或凭据。

## 不做什么

- 不生成初始 Skill，不提供 Skill marketplace，也不管理远程安装。
- 不用自定义关键词路由替代 Claude Code 的原生 Skill 发现。
- 不把评测轨迹、gold 或隐藏材料加入可安装内容。
