# learn-self-evolving-skills

一门让你亲手实现 Skill 自进化系统的工程课程。

课程从电商退货场景出发，构建一条可执行、可评测、可进化、可门控、可回滚的完整链路：

```text
create -> eval -> evolve -> gate -> rollback -> portfolio
```

项目使用 Python 3.11+、Pydantic v2、pytest 和 Claude Code headless。首版只实现硅基流动接入，但保留轻量 Engine 边界，方便以后扩展其他 Provider。

## 当前阶段

仓库已经进入 Phase 0。先运行不消耗 Key 的本地与数据检查：

```bash
python3 scripts/phase0_check.py
```

设置轮换后的 `SILICONFLOW_API_KEY` 后，运行一次完整 smoke：

```bash
python3 scripts/phase0_check.py --live
```

完整 smoke 会验证 Claude Code headless 的硅流模型响应、一次 MCP 调用和 `stream-json` 解析。通过后优先跑通单 case 的完整评测链。

## 文档

- [产品需求](docs/product/prd.md)
- [已确认的产品与架构决策](docs/product/alignment.md)
- [Phase 0 前置检查](docs/phase0-validation.md)
- [开发任务与依赖图](docs/tickets/README.md) / [GitHub Issues](https://github.com/TangWiki-Ai/learn-self-evolving-skills/issues)
- [系统与模块规格](docs/specs/README.md) / [Spec tracking issue](https://github.com/TangWiki-Ai/learn-self-evolving-skills/issues/13)

## 安全

不要把 API Key 写入仓库、配置样例、测试夹具、运行轨迹或报告。真实模型调用只从进程环境读取凭据，默认测试和 CI 不访问付费 API。

## License

课程代码使用 Apache-2.0。上游数据切片保留各自的 MIT License、来源、固定版本和转换记录。
