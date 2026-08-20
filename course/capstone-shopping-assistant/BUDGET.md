# Capstone 预算

| 路径 | 网络与 Key | 发布时费用结论 | 上限处理 |
| --- | --- | --- | --- |
| fixed-v1 | 禁止 | 实际新增付费 `0 CNY` | CI 必须保持 0 |
| live-v1 | 当前禁止 | 未实测，不能估算成事实 | Phase 0 go 后按锁定模型和日期实测 |

fixed-v1 使用课程原创 in-memory fixture。报告中的 synthetic cost 只能用于练习聚合逻辑，
不能写成 Provider spend。

课程总预算目标是不超过人民币 50 元，但当前 live 的授权、服务协议、模型与费用仍未锁定。
因此课程不提供 live 费用承诺。维护者只有在 Phase 0 转为 `go`、独立 experiment root
通过端到端实测后，才能发布带日期、模型版本、币种和换算依据的预算表。

运行途中不能删题来压低费用。需要调整次数或总量时，维护者必须发布新的 profile version，
继续覆盖四类 scenario，并保持 selection/final 私有。
