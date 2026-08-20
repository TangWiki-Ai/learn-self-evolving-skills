# Live setup：当前禁止执行

截至 2026-08-19，Phase 0 source manifest 对代码、数据、商品文本/图片、搜索索引、模型资产、
task 和 persona 的授权都记录为 `unknown`，结论为 `no_go`。课程也没有完成协议、费用和四类
场景的独立 live clean-room 验证。

所以你现在不能运行 live profile，不能设置网络授权，也不能把 fixed 结果描述成真实
ShopSimulator 测量。capstone clean-room 会把 `live.full_workflow` 写成 `blocked`，且不会把
任何 live 命令交给 subprocess。

维护者只有完成以下事项后才能新开规格版本启用 live：

1. 八类资产全部获得可审计的 `verified` 授权结论；
2. 锁定 commit、dataset revision、HTTP protocol 和 error envelope；
3. 完成 reset → search/click/ask → purchase 或安全结束 → terminal/reward → close smoke；
4. 证明 terminal 后不重复 release，且非 terminal 异常只释放当前 session 持有的 episode；
5. 在独立 live experiment root 实测 fresh pair、费用、时间和完整工作流；
6. 通过专门的 live clean-room，不复用 fixed receipts 或 lineage。

即使未来转为 go，live 也必须显式授权并默认串行。fixed 与 live 永远使用不同 experiment
root、不同 lineage，证据不能互相回填。
