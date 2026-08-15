# Phase 0 前置检查

## 运行命令

本地工具和数据检查不调用付费模型：

```bash
python3 scripts/phase0_check.py
```

完整 smoke 只从进程环境读取轮换后的硅流 Key：

```bash
read -s SILICONFLOW_API_KEY && export SILICONFLOW_API_KEY && python3 scripts/phase0_check.py --live
```

命令会静默读取 Key，不把值写进 shell history。不要把 Key 写进 `.env`、Claude 配置、命令脚本、Issue 或聊天。

`--live` 使用 `claude --bare`、临时 `CLAUDE_CONFIG_DIR` 和严格 MCP 配置。它不会读取现有 Claude OAuth、全局 Provider、hooks、plugins、项目说明或持久会话。脚本只在内存中解析 `stream-json`，退出后删除临时 MCP 配置；该配置不含凭据。

主模型默认使用 `deepseek-ai/DeepSeek-V3.2`。你可以通过 `SES_MAIN_MODEL` 临时覆盖模型标识。

## 2026-08-16 实测结果

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| Python | PASS | 3.11.9 |
| Claude Code | PASS | Native CLI 2.1.220，darwin-arm64 |
| Claude 全局配置隔离 | PASS with warning | 全局 Provider host 是 `cmkey.cn`；smoke 使用 `--bare` 和临时配置，不读取它 |
| STATE-Bench | PASS | 固定 commit `5644b1838d96bc4483da29642d058ecaa6f80f7f`；150 个 customer-support tasks，其中 33 个满足 `task_type == return_item`；100 个 train trajectories，其中 21 个对应 return-item tasks |
| ABCD | PASS | 固定 commit `6b8700ce67c6b37b062dd7a60abc76d7ef832a97`；10,042 段对话，其中 1,070 段满足 `scenario.flow == product_defect`；原始、delexed、subflow 字段完整 |
| tau2-bench retail | PASS | 固定 commit `c3398666e6559e3a063da3fc04b5acf7f941464e`；114 tasks；4 个模型结果各含 456 次模拟，共 1,824 trajectories |
| 硅流 endpoint | PASS | `https://api.siliconflow.cn/` TLS 校验通过，未认证根请求返回 HTTP 404，网络可达 |
| 本地 MCP 协议 | PASS | Claude CLI 使用临时配置执行健康检查，`phase0` server 返回 `Connected` |
| 硅流模型响应 | PENDING | 当前进程没有设置轮换后的 `SILICONFLOW_API_KEY`，因此未执行付费调用 |
| 模型驱动 MCP + stream-json | PENDING | 与硅流模型响应共用一次 live smoke；Key 到位后执行 |

ABCD 完整数据 `data/abcd_v1.1.json.gz` 的实测大小为 `36,985,084` bytes，SHA256 为 `2bdf53ac359543dcdc38d55bc6513e78df120363f8f44870716e909f4606de15`。检查脚本日常只读取小样例并用 HTTP metadata 核对完整文件大小，避免反复下载 37 MB。

STATE-Bench 的 33/21 来自 JSON 字段过滤，不来自文件名匹配。后续数据 manifest 必须沿用 `task_type == return_item`，否则数量会错。

## 当前结论

本机运行时和三组官方数据满足开发条件，可以确认这部分 **GO**。硅流 headless、MCP tool calling 和 `stream-json` 仍是一个明确的 **NO-GO/PENDING** 项；设置轮换后的环境变量并让 `--live` 返回 0 后，才关闭 GitHub Issue #1。

首版只验证 Claude Code headless + 硅基流动。代码保留薄 Engine 边界，后续可以增加其他 Provider，但 Phase 0 不实现路由、fallback 或通用 Provider 框架。

## 官方来源

- [STATE-Bench](https://github.com/microsoft/STATE-Bench)
- [ABCD](https://github.com/asappresearch/abcd)
- [tau2-bench](https://github.com/sierra-research/tau2-bench)
- [硅基流动 Claude Code 接入说明](https://api-docs.siliconflow.cn/docs/usercases/use-siliconcloud-in-ClaudeCode)
- [Claude Code headless 说明](https://code.claude.com/docs/en/headless)
