# AGENTS.md

## 当前产品边界

- 用户只通过 `ses journey` 使用仓库。
- instructor Skill 位于 `.agents/skills/self-evolving-skill-instructor/`。
- 8 步 Journey 的真实行为以 `docs/specs/06-course-delivery.md` 和现有测试为准。
- 新用户可以先让 Claude Code 拉取仓库并安装依赖，再由 instructor Skill 接管学习引导。
- 不恢复已删除的旧课程、独立 CLI、自动进化、selection/final、Registry、Portfolio 或兼容层。

## 实现规则

- 使用 Python 3.11+、Pydantic v2、pytest、mypy 和 Ruff。
- CLI 只解析参数和呈现结果；业务逻辑放在对应模块。
- 默认测试使用 fixed 数据和 fake engine，不访问网络或付费 Provider。
- 手动运行 `ses journey station` 时，live 运行必须显式选择 Provider；新用户入口使用 `ses journey start`，它只读取 `ses.json` 的 `default_provider`，不能根据 Key 自动选择或跨 Provider fallback。
- 凭据只从进程环境读取。日志、状态、报告、fixtures 和异常不能包含真实 Key。
- 每个 case 使用隔离工作区；受测 Agent 看不到 gold、其他 case、个人 Skill 或项目源码。
- 只保留当前 Journey 使用的代码、数据、测试和文档。删除功能时同步删除入口、依赖、fixtures 与说明。

## 新用户入口

- 用户提供仓库链接并要求“拉取并安装依赖”时，把仓库放到
  `learn-self-evolving-skills` 目录；目录已存在时先检查，不覆盖用户文件。
- 拉取完成后运行 `uv sync --no-dev --locked`。只有用户要求测试、Lint 或类型检查时，才运行 `uv sync --locked` 安装开发依赖。
- 依赖安装完成后，交给 `.agents/skills/self-evolving-skill-instructor/SKILL.md` 的 `New-user handoff` 完成项目介绍和确认提问。在用户确认前，不读取或要求 API Key，不启动付费 live 运行。
- 用户确认后，Skill 读取 `ses.json` 的 `default_provider`，引导用户在启动 Claude Code 的同一 shell 设置匹配的环境变量，再运行 `uv run ses journey start`。不读取 Key 的值、不把 Key 写入文件、不根据 Key 存在与否切换 Provider。
- 用户中断后，用户再次说“我要学习 Skill 自进化”时，直接恢复 `.ses/status.json` 中保存的 Provider、模型锁和站点。

## 修改前

按任务阅读：

1. `docs/product/prd.md`
2. `docs/specs/00-system-overview.md`
3. 对应模块 spec

## 完成标准

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests scripts
uv run pytest
```

只有用户明确授权后才能运行付费 live smoke。
