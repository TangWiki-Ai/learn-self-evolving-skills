# 第 5 课：从 benchmark 语料挖出可审计候选

## 困惑

一段客服对话不是一道可执行考题。ABCD 提供自然表达和已有意图标签，但没有本课程 Shop 的确定性终态。tau2-bench 提供同一任务的多次运行结果，但把 1,824 条 trajectory 当成 1,824 道题会重复计数。

## 方法

本课完成测试集流水线的前三段：

```text
ABCD exact slice -> Scrub -> Cluster -> label comparison
tau2 trajectories -> group 16 runs by task -> difficulty bucket
                                      |
                                      v
                         stratified candidate list
```

`scripts/prepare_data.py` 先验证固定 commit、MIT License、字节数和 SHA256，再处理数据。Scrub 保留每段 ABCD 对话的 original/delexed 对应关系和 flow/subflow 标签。Cluster 使用本地 TF-IDF，不调用远程 embedding。tau2 输入保持只读，只贡献按 task 去重和难度信号。输出只是候选，不创建 Shop case、gold 或任何 split。

## 业界做法

可靠的数据流水线把来源事实、转换事实和模型推断分开。你应先做精确字段过滤和结构校验，再做语义聚类；最后用上游标签衡量聚类，而不是把聚类编号当成新 gold。难度也应按独立任务统计，不能让同一任务的重复 trial 放大样本量。

## 关键 insight

flow 自评在本切片上没有信息量：全部 1,070 段都是 `product_defect`。subflow 才能检查聚类是否捕捉到 `refund_*`、`return_*` 等差异。参考 full run 的 subflow NMI 为 `0.403582810153`，它描述这次固定数据、scikit-learn `1.9.0`、12 个 cluster 和 seed 0，不是通用质量承诺。

## 固定数据与用途

| 来源 | 固定版本 | 许可 | 本课用途 |
|---|---|---|---|
| ABCD 角色扮演 benchmark | `6b8700ce67c6b37b062dd7a60abc76d7ef832a97` | MIT | original/delexed 清洗、flow/subflow 聚类自评 |
| tau2-bench retail | `c3398666e6559e3a063da3fc04b5acf7f941464e` | MIT | 按 task 去重、按 16 次运行通过率分层 |
| STATE-Bench | `5644b1838d96bc4483da29642d058ecaa6f80f7f` | MIT | 只给候选提供可执行任务语义；本课不生成 case |

机器可读来源、下载 SHA256、切片条件、结果生成 commit 和转换步骤位于 [`data/upstream/manifest.json`](../../data/upstream/manifest.json)。原许可证位于 [`data/upstream/abcd/LICENSE`](../../data/upstream/abcd/LICENSE)、[`data/upstream/tau2/LICENSE`](../../data/upstream/tau2/LICENSE) 和 [`data/upstream/state_bench/LICENSE`](../../data/upstream/state_bench/LICENSE)。课程只按 benchmark 或角色扮演数据使用这些材料。

## Starter

[`starter/mining.py`](starter/mining.py) 留下三个学习缺口：

1. `scrub_product_defect`：按 `scenario.flow == product_defect` 精确切片，验证并保留 original/delexed 对齐。
2. `cluster_and_compare_labels`：通过可注入 adapter 聚类，再分别和 flow、subflow 比较。
3. `aggregate_tau2_by_task`：验证四份结果、每份四个 trial，先聚合成每题 16 次运行，再计算难度。

[`solution/mining.py`](solution/mining.py) 连接生产模块，不复制另一套简化算法。

## 实现任务

1. 不改写 ABCD intent。为清洗后的记录生成稳定 source ID，并保留原文、脱敏文本、split、flow 和 subflow。
2. 让聚类 adapter 可注入。测试使用 deterministic fake；完整运行使用锁版本的本地 scikit-learn adapter。
3. 分别记录 flow 和 subflow 的 contingency、ARI、NMI、homogeneity、completeness 与 V-measure。单标签 flow 必须明确标记为不具信息量。
4. 用 `(result_asset_id, task_id, trial)` 去重 tau2 运行键。每个 task 必须恰好聚合 16 次，不允许把 trajectory 直接变成候选。
5. 候选清单保留长尾意图与难度来源，但保持 `executable=false`，也不能写 creator、selection 或 final。

## 运行

五分钟离线路径只使用签入的小 fixture，不联网、不读取 Key：

```bash
uv run python scripts/prepare_data.py --profile fixture \
  --output .ses/lesson05-fixture --clusters 2 --seed 0
uv run pytest course/ch05-mine-benchmark-data/tests
```

如果你已经取得并校验 full 资产，可以离线重跑完整流水线：

```bash
uv run python scripts/prepare_data.py --profile full \
  --output .ses/lesson05-full-existing --clusters 12 --seed 0
uv run python course/ch05-mine-benchmark-data/scripts/build_reference.py \
  --bundle .ses/lesson05-full-existing \
  --output .ses/lesson05-full-reference.json
```

全新环境必须显式允许网络，脚本才会下载固定资产。下载步骤只安装并校验
pinned 资产，不运行生成器，也不复用上一个 full bundle 的输出目录：

```bash
uv run python scripts/prepare_data.py --download-full --allow-network \
  --download-only --profile full
```

下载器会先写临时文件，校验 byte count 和 SHA256 后再原子安装。下载成功后，
你可以运行上一段 full 命令，把结果写到新的 `.ses/` 目录。网络失败时命令会失败
并保留已有已验证资产；它不会生成一份假的 full 结果。

## 对照产物

[`full-funnel-reference.json`](full-funnel-reference.json) 由当前已校验的完整上游资产实际生成。2026-08-19 的 seed 0 运行得到：

- ABCD：10,042 段来源对话中精确选中 1,070 段 `product_defect`；1,070 段都保留 original/delexed，合计各 28,535 turns，speaker 和位置全部对齐。
- split：train 863、dev 102、test 105。
- subflow：return_size 191、return_color 180、refund_status 179、refund_update 177、refund_initiate 176、return_stain 167。
- Scrub：空值、错位、非法编码、字面重复都为 0；1,070 段进入聚类和候选池。
- tau2：1,824 条运行按 114 个 task 聚合，每题 16 次；hard 10、medium 34、easy 70。脚本完成后再次校验 5 份 tau2 输入资产，SHA256 全部不变。

产物还保存 upstream manifest hash、源 commit、License hash、输入和输出 SHA256、转换版本、adapter 版本、label metrics 与 tau2 结果生成 commit。它不包含完整上游对话或 trajectory。

## 预算

fixture 和 full 处理都不调用模型，新增付费费用为 ¥0。fixture 不使用网络。完整 pinned 资产合计 131,298,889 bytes（约 131.3 MB）；只有 `--download-full --allow-network` 会产生下载流量。本次本地 full 处理约 40 秒，实际时间取决于 CPU 和磁盘。

## 拓展阅读

- 阅读 [`docs/specs/05-testset-pipeline.md`](../../docs/specs/05-testset-pipeline.md) 的 Implementation Decisions。回答：为什么 ABCD 只能提供“考什么”，不能提供 Shop gold？
- 阅读 [`docs/specs/10-cross-module-contracts.md`](../../docs/specs/10-cross-module-contracts.md) 的 serialization rules。回答：哪些版本或 adapter 变化必须改变 artifact hash？
- 阅读 [ABCD 论文](https://aclanthology.org/2021.naacl-main.239/) 的 Dataset 章节。回答：original/delexed 对应关系为什么适合清洗自检？
- 阅读 [tau2-bench 论文](https://arxiv.org/abs/2506.07982) 的实验设置。回答：为什么同任务多 trial 应先聚合，再进入难度分层？
