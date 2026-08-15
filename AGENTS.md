# AGENTS.md

本仓库由多个 AI Agent 并行开发。你必须先确认当前 ticket、依赖和文件所有权，再编辑代码。

## 开始前

按顺序阅读：

1. `docs/product/alignment.md`
2. `docs/specs/00-system-overview.md`
3. `docs/specs/10-cross-module-contracts.md`
4. 当前任务对应的模块 spec
5. 当前 GitHub Issue 的正文和阻塞关系
6. `docs/development/parallel-implementation.md`

产品决策冲突时，`alignment.md` 优先于旧 PRD。GitHub Issues 是任务状态的唯一来源。Spec 定义模块长期行为，Issue 定义本次实际交付；不要把整份 spec 当成一个并行任务。

## 并行规则

- 每个 Agent 使用独立 branch 和 worktree。平台已经隔离 worktree 时，直接使用平台提供的目录。
- 只修改并行实施文档分配给你的路径。共享配置、CLI 入口、contracts 和锁文件由指定 owner 修改。
- 跨模块记录只保留一个 canonical model。需要改接口时，提交 contract proposal，不要在自己的模块复制一份相似类型。
- 从最新 `origin/main` 开始。交付前重新同步 main，处理自己分支中的冲突并运行完整检查。
- Agent 提交自己的 branch，不直接合并或推送 `main`，不关闭集成 Issue。
- 发现其他 Agent 的改动时保留并适配，不回滚、不覆盖。

## 工程规则

- 使用 Python 3.11+、PEP 621、`src/` layout、Pydantic v2、pytest、mypy 和 Ruff。
- 首版 CLI 使用标准库 `argparse`。业务逻辑放在模块中，CLI 只做参数解析、调用和呈现。
- 跨模块持久记录遵守 cross-module contracts；代码标识、schema 字段和注释使用英文。
- 默认测试使用固定 fixture 和 fake engine，不访问网络，不读取付费 Key。真实 Provider 只在显式 live smoke 中运行。
- 凭据只从进程环境读取。日志、异常、报告、fixtures、命令参数和 Git 历史都不能出现真实 Key。
- 数据只使用 PRD 指定的固定上游版本。公开文字称其为 benchmark 或角色扮演数据。
- 保持实现聚焦当前 ticket。未来 Provider 只保留薄 Engine seam 和文字说明，不提前实现路由框架。

## 完成标准

Bootstrap 合并后，交付前至少运行：

```bash
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```

涉及 live 路径时，默认测试仍需离线通过。只有明确授权后才能另跑 live smoke。

Handoff 必须列出：branch、commit、修改文件、验收标准、测试命令与结果、contract 变更、未解决风险。明确写出没有运行的检查及原因。
