# AGENTS.md

## 当前产品边界

- 用户只通过 `ses journey` 使用仓库。
- instructor Skill 位于 `.agents/skills/self-evolving-skill-instructor/`。
- 8 步 Journey 的真实行为以 `docs/specs/06-course-delivery.md` 和现有测试为准。
- 不恢复已删除的旧课程、独立 CLI、自动进化、selection/final、Registry、Portfolio 或兼容层。

## 实现规则

- 使用 Python 3.11+、Pydantic v2、pytest、mypy 和 Ruff。
- CLI 只解析参数和呈现结果；业务逻辑放在对应模块。
- 默认测试使用 fixed 数据和 fake engine，不访问网络或付费 Provider。
- live 运行必须显式选择 Provider。系统不能根据 Key 自动选择或跨 Provider fallback。
- 凭据只从进程环境读取。日志、状态、报告、fixtures 和异常不能包含真实 Key。
- 每个 case 使用隔离工作区；受测 Agent 看不到 gold、其他 case、个人 Skill 或项目源码。
- 只保留当前 Journey 使用的代码、数据、测试和文档。删除功能时同步删除入口、依赖、fixtures 与说明。

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
