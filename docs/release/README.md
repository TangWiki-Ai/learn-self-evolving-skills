# 本地发布验证

`scripts/validate_release.py` 只读取仓库和你显式传入的临时产物。它不会下载数据、调用 Provider、读取 final runner 或替你填写人工结论。

先运行静态检查：

```bash
uv run python scripts/validate_release.py
```

需要同时执行十课独立测试时运行：

```bash
uv run python scripts/validate_release.py --run-course-tests
```

退出码 `0` 表示全部通过，`2` 表示只剩明确 deviation，`1` 表示至少一个失败。你也可以加 `--json`，或用 `--output PATH` 保存不含本机仓库绝对路径的 JSON 报告。

静态门禁会检查十课结构和本地链接、数据来源与许可证、完整资产 checksum、四个 split 的
四维互斥、tracked release 内容中的 source ID/hidden gold 泄漏、凭据与本机路径、L1/L2/L3
自包含和体积、成本口径、历史 live 证据边界及集中人工 packet。扫描器从 Git index 读取全部
tracked 文件，不按扩展名筛选，也不跳过 starter、solution、tests 或 scripts。没有 Git 元数据
的 clean-room 副本会扫描物化出的 release 树，仅排除 `.git`、`.venv` 和工具 cache 等生成
基础设施。当前 binary allowlist 为空；任何 tracked 文件只要不能严格按 UTF-8 读取，安全检查
就会失败。holdout schema 实现和对应单测只允许精确匹配的结构性 fixture 行；source ID 没有
豁免。

## Protected holdout

公开仓库只保存不暴露题目身份的 selection/final manifest 和 commitment，不跟踪逐题请求
`data/testset/protected/public/**` 或私有 `data/testset/protected/private/**`。因此不传外部
bundle 时，验证器只校验公开文件的 schema、固定数量、opaque lock 和 commitment，然后明确
返回 deviation。它不会把缺少 source ID、semantic group、case ID、content hash、fixture、
oracle 和 rubric 的检查写成 PASS。

第一次构造时，你必须把锁定的 STATE-Bench source tar、至少 32-byte 的 ranking key 和受保护
semantic-group mapping 都放在仓库外。key 与 mapping 文件必须为 `0600`；输出必须是新的空目录：

```bash
uv run python scripts/build_holdout_assets.py \
  --archive /private/tmp/state-bench-source.tar.gz \
  --creator-seed-manifest data/skill-v0/creator/seed-manifest.json \
  --develop-manifest data/testset/ticket07/generated/develop-manifest.json \
  --ranking-key-file /private/tmp/holdout-ranking.key \
  --semantic-group-map-file /private/tmp/holdout-semantic-groups.json \
  --output /private/tmp/ses-protected-holdout
```

builder 会把 key 和 mapping 的规范化副本写入 bundle 的 `private/`，inventory 用 path 和 SHA256
绑定 mapping。原始输入、bundle 副本和逐题文件都不能进入 Git。要复核完整四维互斥和上游
派生，再显式运行。路径不能经过 symlink ancestor；macOS 上请直接使用 `/private/tmp`，不要使用
指向它的 `/tmp` 别名：

```bash
uv run python scripts/validate_release.py \
  --protected-holdout-root /private/tmp/ses-protected-holdout \
  --state-bench-archive /private/tmp/state-bench-source.tar.gz
```

外部 bundle 的 selection/final manifest 和 commitment 必须逐字节匹配公开仓库；bundle 根目录
应为 `0700`，选题 key、semantic mapping 和全部逐题文件应为 `0600`。完整验证会继续检查
creator/develop/selection/final 按 source ID、semantic group、case ID 和 content hash 互斥，
并用 source tar 加 bundle 内受 commitment 约束的 protected mapping 与 ranking key 复现 fixture、
oracle、rubric、公开请求和选题排序。只传外部 bundle、不传或找不到 source tar 时，四维互斥
可以通过，但整体仍保留 source-tar deviation。

注入 external bundle 后，发布扫描器还会从同一个 descriptor-bound snapshot 读取已选 source
ID、逐题公开请求和 content hash，与 tracked release 文本逐一比对。接口只返回命中的仓库相对
路径，不返回受保护值；任何命中都使发布验证失败。

同一 bundle 也可以交给 develop 的受信持久化前 verifier：

```bash
uv run ses qualify-cases \
  --protected-holdout-root /private/tmp/ses-protected-holdout \
  --output /private/tmp/ses-qualified-develop \
  --json
```

该命令只输出 aggregate validation status 和 inventory commitment hash，不输出受保护身份。
不传 bundle 的 fixed/offline 课程命令会明确返回 `fixed_offline_unverified`；live 缺 bundle 时
会在读取 Provider 凭据和创建输出前关闭。

当前 STATE-Bench return 池排除 creator 和已占用语义组后只剩 19 个 eligible group，而
selection+final 使用其中 18 个。protected mapping、eligible membership、精确排名、split、
逐题身份和 gold 都不公开；但上游 33-task return source universe 本身公开且很小，18/19 的
使用比例也过高。发布文档因此不声称强抗污染 secrecy；扩大 source pool 或加入经过验证的
keyed policy variants 仍是后续工作。

## Full 数据重复生成

在两个不同的临时目录中，用相同的锁定依赖和参数运行：

```bash
uv run python scripts/prepare_data.py --profile full --output /tmp/ses-full-run-1 --clusters 12 --seed 0
uv run python scripts/prepare_data.py --profile full --output /tmp/ses-full-run-2 --clusters 12 --seed 0
uv run python scripts/validate_release.py \
  --full-data-bundle /tmp/ses-full-run-1 \
  --full-data-bundle /tmp/ses-full-run-2
```

验证器会先检查每个 bundle 的内部 byte count 和 SHA256，再比较两次输出及 [`full-funnel-reference.json`](../../course/ch05-mine-benchmark-data/full-funnel-reference.json) 的七项 inventory。只提供一次运行时，它会保留 deviation，不会声称已经证明重复性。

PASS 还要求参考事实和生成 funnel 同时匹配：ABCD 必须从 10,042 段精确取得 1,070 段
`product_defect`，original/delexed 各保留 28,535 turns 及既定 split/subflow 分布；tau2
必须把 1,824 runs 只读聚合成 114 tasks、每题 16 runs，并保持 hard/medium/easy 为
10/34/70。只对上 hash、但数量或语义摘要漂移时，验证器仍会失败。

## README 命令证据

验证器会解析根 README 与十课 README 的 fenced shell block 和内联 `uv run` 命令。
根命令使用 `root:` ID，课程命令使用 `lesson-NN:` ID，二者不会混淆。静态解析只检查
CLI、脚本和测试目标是否存在；它不能冒充 clean-room 执行。

提交稳定后，用新目录运行机械执行器。它通过 `git archive HEAD` 只物化当前 commit
中的普通文件，先核对 archive inventory 与 `git ls-tree` 完全一致，再安全解包。它拒绝
符号链接、硬链接、重复路径和路径逃逸；不会继承 ignored/untracked 文件、历史 runs、cache、
本机下载或 `.env`/`.env.*`。随后它执行 `uv sync --all-extras --locked`，再按 README
fenced block 顺序运行。一个 block 只用一个 shell，所以 Lesson 9 的变量赋值会保留到
promote/rollback。执行器不向子进程传递凭据；显式 live、网络下载和依赖未提交 full assets
的 Lesson 5 block 只记录 deviation。两套显式 `--full-data-bundle` 仍单独验证 full 结果。

```bash
uv run python scripts/run_clean_room_release.py \
  --workspace /tmp/ses-release-clean-room \
  --output /tmp/ses-release-command-evidence.json
```

默认拒绝 dirty source。`--allow-dirty-source` 只供开发诊断；它生成的 evidence 会被发布
验证器拒绝，不能作为 release PASS。

你可以用 `--command-evidence PATH` 传入以下结构。`repository_commit` 不仅要是完整的
40 位 commit，还必须精确等于待验证仓库的当前 `HEAD`；旧 commit 的 evidence 会直接失败。
每条记录用 `command_id` 绑定 README scope、行号和命令摘要，同时保存完整命令 SHA256。
相同命令可以出现在根 README 和课程 README，但两条记录必须保留各自的 `command_id`；重复、
缺失、错 scope 或错 hash 都会失败。`deviation` 必须写原因，不能改成 `passed`。

```json
{
  "schema_version": "v1alpha1",
  "record_type": "clean_room_command_evidence",
  "environment_kind": "fresh_clone",
  "repository_commit": "0000000000000000000000000000000000000000",
  "source_clean": true,
  "source_materialization": "git_archive_head_regular_files",
  "shell_grouping": "readme_fenced_blocks",
  "credential_environment_names": [],
  "locked_sync": {
    "command": "uv sync --all-extras --locked",
    "status": "passed"
  },
  "commands": [
    {
      "command_id": "root:line-1:000000000000",
      "command_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "readme": "README.md",
      "line": 1,
      "status": "deviation",
      "exit_code": null,
      "reason": "canonical Provider balance unavailable"
    }
  ]
}
```

## 人工边界

[`human-review-packet.md`](human-review-packet.md) 集中列出 Lesson 3 标签、9 条 creator、15 条 develop case 和 PRD 首发前 12 项。模板不预选任何决定。你必须直接检查证据并签署；AI 委托记录不能满足人工门禁。

[`../phase0-validation.md`](../phase0-validation.md) 只保存 2026-08-16 的历史 Provider smoke。
本轮没有复测 canonical live 时，根 README 必须明确写 `live_not_rerun`（或同义的“本轮未复测”）；
验证器不会把历史 PASS 当作当前 release PASS。
