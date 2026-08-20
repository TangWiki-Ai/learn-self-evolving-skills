# ShopSimulator Phase 0 来源、授权与协议核查

> 核查日期：2026-08-19
>
> 核查人：Codex `phase0_research` 子任务（来源审计，不构成法律意见）
>
> 上游固定提交：`51bb26012cee31aea7ac26177c5ffe807026ac07`
>
> 结论：`live_decision = no_go`；`fixed_decision = continue`
>
> 公开名称：`ShopSimulator-inspired fixed workflow`

## 1. 结论

当前不能公开发布 ShopSimulator live 课程能力。

至少有六项 live 必需资产或条件仍为 `unknown`：ShopSimulator 仓库代码、Hugging Face 数据集、商品文本、商品图片、任务、persona，以及由这些数据生成的搜索索引。固定提交的 167 个 tracked files 中没有 `LICENSE`、`COPYING` 或 `NOTICE`。Hugging Face 数据集元数据也没有 license tag、dataset card 或 LICENSE 文件。[固定提交树](https://github.com/ShopAgent-Team/ShopSimulator/tree/51bb26012cee31aea7ac26177c5ffe807026ac07)（访问日期：2026-08-19）；[HF 官方数据集元数据](https://huggingface.co/api/datasets/wpei/ShopSimulator)（访问日期：2026-08-19）。

论文说明作者获得电商平台授权来收集商品信息，也说明 persona 由模型生成后经人工修订，不包含真实用户隐私信息。但论文没有公布授权文本、授权范围或给下游课程的再许可。你不能从“作者获准收集”推导出“课程获准本地执行、公开截图、摘要原始内容或再分发”。[论文 §2.2](https://arxiv.org/html/2601.18225v1#S2.SS2)；[论文 Ethics Statement](https://arxiv.org/html/2601.18225v1#A1)（访问日期：2026-08-19）。

固定提交也不能按 README 独立复现。`setup.sh` 进入该提交不存在的 `shop_env/search_engine/`，runtime 又读取不存在的 `shop_env/data/items_eval_train.json`；仓库实际只带 `fine_items_eval_train_all.json.gz`。实际 YAML 把 Agent `source` 写成 `openai`，但 Agent 代码只接受 `idealab`；standard single 配置还把该行写成缺少空格的 `source:openai`。[setup.sh](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/setup.sh#L12-L17)；[utils.py](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/web_agent_site/utils.py#L7-L10)；[single 配置](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/single_eval/configs/standard/qwen3_235b.yaml#L1-L8)；[single Agent](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/single_eval/agent.py#L37-L61)（访问日期：2026-08-19）。

因此，本核查不启动上游服务，不调用模型，不抓取商品图片，也不生成或发布任何上游任务、persona、商品或索引副本。项目可以继续完成原创 fixed/in-memory 路线；只有在所有 live 必需资产变为 `verified`，且完成真实协议与费用 smoke 后，维护者才能重新评估 `go`。

## 2. 核查范围与方法

本核查只使用以下一手来源：

- [ShopSimulator 固定提交](https://github.com/ShopAgent-Team/ShopSimulator/tree/51bb26012cee31aea7ac26177c5ffe807026ac07)；
- [ShopSimulator 论文 v1](https://arxiv.org/html/2601.18225v1)；
- [wpei/ShopSimulator 官方 Hugging Face 页面](https://huggingface.co/datasets/wpei/ShopSimulator)及官方 API；
- 资产拥有方的官方页面：Qwen、spaCy、WebShop 和 SiliconFlow。

除非段落另有说明，下文所有链接的访问日期都是 2026-08-19。

核查动作：临时读取官方 Git 对象、枚举树、计算本地只读副本的 SHA-256、静态检查协议和 reward、读取官方元数据。没有把上游代码、数据、任务、persona、商品、图片或索引写入本仓库。本文只保存 hash、数量、协议事实、风险和链接。

状态含义：

- `verified`：一手来源给出明确身份或授权条款；
- `unknown`：一手来源没有给出足以覆盖课程用途的条款，或 runtime 身份没有锁定；
- `prohibited`：一手来源明确禁止目标用途。

本轮没有发现可直接归类为 `prohibited` 的条款。`unknown` 已足以触发 fail-closed `no_go`。

## 3. 固定身份与校验和

### 3.1 Git 身份

| 字段 | 值 |
| --- | --- |
| repo | `https://github.com/ShopAgent-Team/ShopSimulator` |
| commit | `51bb26012cee31aea7ac26177c5ffe807026ac07` |
| parent | `34b2efe1e5da0933ffed01f9b006b997eb2ae087` |
| tree | `4e5d7a14bf3004af7d0066f9f5052e7ef393a5c1` |
| commit date | `2026-01-28T00:29:18+08:00` |
| subject | `Delete shop_env/README.md` |
| tracked files | `167` |
| deterministic `git archive` SHA-256 | `a06e15e7d317fbfee9738cabbad0b0977624767cff9ad7a2ef90995539f2d306` |

来源：[固定提交](https://github.com/ShopAgent-Team/ShopSimulator/commit/51bb26012cee31aea7ac26177c5ffe807026ac07)（访问日期：2026-08-19）。

### 3.2 协议与 reward 关键文件

| 文件 | SHA-256 |
| --- | --- |
| `README.md` | `0bea36b2f5e577476c8d59279958142b92b9260e87a50dc94b29483b4e78399f` |
| `get_score.py` | `2f8924833fba61bd2cbc704f05ee1ad7d6cfd396cd9148e076f19f6fc9aadbca` |
| `shop_env/setup.sh` | `beeae9ce8dde5adc319918400df0a83490d0e9221a5bc283e7967e4a94071faf` |
| `shop_env/requirements.txt` | `361d825082889d7e18aaed29b450a5fa707207ac0ea589a27608c9566b1ddeb7` |
| `shop_env/shop_env/pack_api.py` | `f2fdaa63b5e9e66107962c27036ea5bd1c7e564c9e647241f55c984f0e91ceba` |
| `shop_env/shop_env/shop_agent.py` | `72c269d3dabafac7e06ca45cc94fd5e3f2d0bfe044bf37a7d72f062e68087052` |
| `shop_env/web_agent_site/utils.py` | `7f0bb9ca2bd4710afa13d290b786ac041aee7dadd6c055360dab9620521074f4` |
| `shop_env/web_agent_site/engine/engine.py` | `846be98541b0c525898405eb32e6aef724b6c7cfacdd36ff5d35be338aa63cd7` |
| `shop_env/web_agent_site/engine/goal.py` | `d217be9e428546fc1a8fb852cf6a421a5e4b9c4695e20f3c2c09bf5f5e76e5f4` |
| `shop_env/web_agent_site/envs/web_agent_text_env.py` | `ba9798e5c99de6880fbbefe6788abf0a800bb7ff34d1b6a23b12632a91abe122` |
| `single_eval/env.py` | `dfdeb036775e16787d0b494043f95652ef4874bb0e4043713d2504afa326ad91` |
| `multi_eval/env.py` | `5253908daeae393222dcb7fd5faff29897db22536a1a8d0e188ecaf762855588` |
| `multi_eval/shopper.py` | `cb692589a54e739a6231fce41b32b8266fb9a76c9550c0675a91d9433759d617` |
| `multi_eval/configs/standard/qwen3_8b.yaml` | `4f299d87a92b71b71f46f75713534fba0e7f9dddd25114940fc1f768aa841c26` |
| `multi_eval/configs/persona/qwen3_8b.yaml` | `d59b264635f1b9d3a3c7bc6dcdb9e93a7ed0a0aaff6c83283cccd5021739cb09` |
| `single_eval/configs/standard/qwen3_235b.yaml` | `256df1c17dddd5735c9ca90bba34b667e080f8b97b875ffaad1553b935be840a` |
| `single_eval/configs/persona/qwen3_235b.yaml` | `96f6f43d30f360e550b5382e86c7a932554041a2c01d06fdda6b8081348a4f3d` |

这些 hash 来自上表固定 Git tree 的 blob 内容；它们不授予使用权。

### 3.3 数据身份

固定提交包含 `shop_env/data/fine_items_eval_train_all.json.gz`：

- Git blob：`850e7d19558ebb609bbc184a7fa788e6b1075edd`；
- 压缩文件 SHA-256：`f51c33217061479f9c95a1068621fcd38e4883ae3d2f6a1627037bea934f2125`；
- 解压字节 SHA-256：`57b10950a0064d16c81535a1d764a75879a508d250dde8a2a1787c5e6045559f`；
- 压缩大小：`19,923,433` bytes；解压大小：`140,260,986` bytes；
- 只读聚合：23,421 个商品记录、23,421 条 instruction；其中 `eval=1,459`、`train=21,962`，非空 persona 记录共 4,666。

来源：[固定提交数据路径](https://github.com/ShopAgent-Team/ShopSimulator/tree/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/data)（访问日期：2026-08-19）。本文没有保存任何记录内容。

固定提交 README 只链接可变的 HF `main`，没有锁 revision 或 checksum。[README](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/README.md#L13-L21)（访问日期：2026-08-19）。2026-08-19 观察到的 HF revision 是 `ab34b2898059b21e09794de17e61684f1baeb50d`，官方 API 列出六个文件且没有 README/LICENSE：

| HF 文件 | 身份 |
| --- | --- |
| `.gitattributes` | Git oid `69389c8e34d603bd4ae8022f78b57049728c5195` |
| `fine_items_eval_persona.jsonl` | Git oid `b2f850d9efa84e3956439e7855e8cda97585d1a1` |
| `fine_items_eval_standard.jsonl` | Git oid `cb8fea02fb8e976bf4df2bce0a44859a8028cfd3` |
| `fine_items_eval_train_all.jsonl` | LFS SHA-256 `8a817cfcd19395ec57f195a93c5f07bd987b920b8345015407d8ce7e26188bad` |
| `fine_items_train_persona.jsonl` | LFS SHA-256 `62cbca38719a17f7b966ebc301ac5bb432ccd7a574f142dd37539a626d448ec3` |
| `fine_items_train_standard.jsonl` | LFS SHA-256 `16001864a43c392d58116bf280c6784511997bc422b8c43ddf59a4e08c514587` |

来源：[HF revision 文件元数据](https://huggingface.co/api/datasets/wpei/ShopSimulator/tree/ab34b2898059b21e09794de17e61684f1baeb50d?recursive=true&expand=true)（访问日期：2026-08-19）。仓库 gzip 与 HF JSONL 的 byte identity 不同；没有上游 manifest 证明两者的语义映射。本核查不据此断言数据内容不同，只把映射标为未锁定。

### 3.4 论文与模型身份

- 论文：`arXiv:2601.18225v1`。[官方页面](https://arxiv.org/abs/2601.18225v1)（访问日期：2026-08-19）。
- Qwen 官方模型：`Qwen/Qwen3-235B-A22B-Instruct-2507`，HF revision `ac9c66cc9b46af7306746a9250f23d47083d689e`，license `Apache-2.0`。[官方模型元数据](https://huggingface.co/api/models/Qwen/Qwen3-235B-A22B-Instruct-2507)；[固定 revision](https://huggingface.co/Qwen/Qwen3-235B-A22B-Instruct-2507/tree/ac9c66cc9b46af7306746a9250f23d47083d689e)；[LICENSE](https://huggingface.co/Qwen/Qwen3-235B-A22B-Instruct-2507/blob/ac9c66cc9b46af7306746a9250f23d47083d689e/LICENSE)（访问日期：2026-08-19）。
- spaCy `zh_core_web_sm` 官方模型标记为 MIT，但上游 `setup.sh` 使用不带版本或 revision 的下载命令。[官方模型页](https://huggingface.co/spacy/zh_core_web_sm)；[官方 LICENSE](https://huggingface.co/spacy/zh_core_web_sm/blob/main/LICENSE)（访问日期：2026-08-19）。

## 4. Asset-level rights manifest

表中的“允许”只表示一手来源明确覆盖。`unknown` 一律按“不进入公开 live 发布”处理。

| 资产 | 状态 | 条款与来源 | 本地执行 | 公开截图 | 公开摘要 | 再分发 |
| --- | --- | --- | --- | --- | --- | --- |
| ShopSimulator 仓库代码与配置 | `unknown` | 固定树没有 LICENSE/COPYING/NOTICE。[固定树](https://github.com/ShopAgent-Team/ShopSimulator/tree/51bb26012cee31aea7ac26177c5ffe807026ac07) | 未确认 | 未确认 | 只发布本文这类事实性审计与链接，不复制代码 | 未确认 |
| WebShop 来源代码片段 | `unknown`（对 ShopSimulator 副本） | ShopSimulator 的 transfer README 明确指向 WebShop；WebShop 原仓库为 MIT，但 ShopSimulator 没有逐文件 provenance、版权声明或 MIT notice，MIT 不能覆盖 ShopSimulator 自有修改。[transfer README](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/transfer/README.md)；[WebShop LICENSE](https://github.com/princeton-nlp/WebShop/blob/master/LICENSE.md) | 不能据此给整个 ShopSimulator 放行 | 未确认 | 可说明来源关系 | 只有完成逐文件 provenance 和 attribution 后才能另审 |
| HF 数据集整体 | `unknown` | 官方元数据没有 license tag，revision 文件中也没有 card/LICENSE。[metadata](https://huggingface.co/api/datasets/wpei/ShopSimulator)；[tree](https://huggingface.co/api/datasets/wpei/ShopSimulator/tree/ab34b2898059b21e09794de17e61684f1baeb50d?recursive=true&expand=true) | 未确认 | 未确认 | 只引用论文聚合事实 | 未确认 |
| 商品文本、属性、价格、店铺名 | `unknown` | 论文称来自 2025-06 Taobao snapshot；Ethics 只确认作者收集获授权，没有下游许可文本。[论文 §2.2](https://arxiv.org/html/2601.18225v1#S2.SS2)；[Ethics](https://arxiv.org/html/2601.18225v1#A1) | 未确认 | 未确认 | 可引用论文统计；不能发布原始商品示例或改写数据集 | 未确认 |
| 商品图片与外部图片 URL | `unknown` | HF 官方预览显示数据包含 `img.alicdn.com` URL；没有图片许可或课程截图许可。[HF 数据页](https://huggingface.co/datasets/wpei/ShopSimulator) | 不抓取 | 不允许进入公开课程，直至 verified | 只能说明“数据含图片 URL” | 未确认 |
| task / instruction | `unknown` | 论文称标注员基于商品元数据撰写 24K task，但没有 task license。[论文 §2.2 Task Construction](https://arxiv.org/html/2601.18225v1#S2.SS2) | 未确认 | 未确认 | 可引用数量与构建方法；不发布题目文本 | 未确认 |
| persona / revised instruction | `unknown` | 论文称 LLM 起草、人工修订 4,726 个 profile，并称不含真实用户隐私；没有 persona license。[论文 §2.2 Personalization](https://arxiv.org/html/2601.18225v1#S2.SS2)；[Ethics](https://arxiv.org/html/2601.18225v1#A1) | 未确认 | 不发布原始 profile | 可发布脱敏的论文级方法摘要 | 未确认 |
| Shopper prompt/config 与示例输出 | `unknown` | 它们位于无许可证的固定仓库，且示例输出派生自未知授权 task、persona 和商品。[multi config](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/multi_eval/configs/standard/qwen3_8b.yaml) | 未确认 | 未确认 | 只概括协议行为 | 未确认 |
| Lucene 搜索索引 | `unknown` | 固定提交不含 `search_engine/` 或 index；索引由未知授权商品数据派生。`setup.sh` 的构建入口无法在该 tree 中执行。[setup.sh](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/setup.sh#L12-L17)；[shop_env tree](https://github.com/ShopAgent-Team/ShopSimulator/tree/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env) | 不可复现，且权利未确认 | 不适用 | 可说明索引缺失 | 未确认 |
| Qwen3-235B-A22B-Instruct-2507 权重 | `verified` | 官方 HF revision `ac9c66...`，Apache-2.0。[LICENSE](https://huggingface.co/Qwen/Qwen3-235B-A22B-Instruct-2507/blob/ac9c66cc9b46af7306746a9250f23d47083d689e/LICENSE) | 允许，遵守 Apache-2.0 | 不适用 | 允许说明模型身份 | 允许，须满足 Apache-2.0 条件 |
| spaCy `zh_core_web_sm` | `verified`（license）；`unknown`（runtime revision） | 官方模型 MIT；上游没有 pin 版本。[官方模型](https://huggingface.co/spacy/zh_core_web_sm) | MIT 允许；当前命令不可复现锁定 | 不适用 | 允许说明身份 | MIT 允许并须保留 notice |
| live Provider 服务绑定 | `unknown` | 上游 YAML 使用 `{url}`、`{your key}` 和非 provider-qualified model alias；没有 provider revision、服务条款 hash 或账单证据。[multi config](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/multi_eval/configs/standard/qwen3_8b.yaml#L1-L32) | 未锁定 | 不适用 | 可说明缺口 | 不适用 |

`terms_sha256` 对所有 `unknown` 项均为 `null`：上游没有提供可计算 hash 的许可文本。发布前不能用 repo URL、论文 citation 或公开下载状态替代条款 hash。

## 5. 固定协议核查

### 5.1 论文协议

论文定义了四种场景：single、single with personalization、multi、multi with personalization。Agent 可以搜索、浏览结果、查看详情并购买；multi 额外允许与 shopper 对话。论文说 episode 在购买或达到最大 action step 后结束，single 最多 30 step，multi 最多 40 step。[任务与动作](https://arxiv.org/html/2601.18225v1#S2.SS1)；[四场景与 step 上限](https://arxiv.org/html/2601.18225v1#S3.SS1)（访问日期：2026-08-19）。

### 5.2 固定 commit 的 HTTP 协议

唯一 HTTP endpoint 是 `POST /api/shop_agent`。外层请求接受：

| action | 输入 | 主要返回 |
| --- | --- | --- |
| `reset` | `idx`；不带 `env_idx` 时从池分配 | `instruction`、`instruction_simple`、`goal_options`、`env_idx`、`idx`，可选 `user_persona`、`reason_key` |
| `interact` | `env_idx`、`response` | `done`、`reward`、observation、`reward_detail`、`purchase`、`goal`、`over` |
| `release_one` | `env_idx` | message 或 error |
| `release_all` | 无 | message |

来源：[pack_api.py](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/shop_env/pack_api.py#L41-L113)；[shop_agent.py reset](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/shop_env/shop_agent.py#L22-L48)；[shop_agent.py interact](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/shop_env/shop_agent.py#L86-L147)（访问日期：2026-08-19）。

`response` 内部只解析 `search[...]` 和 `click[...]`。`ask_shopper` 不是环境 API action；multi runner 在进程内调用另一个 LLM 完成 shopper 对话。因此，SES live Adapter 不能只代理环境 endpoint，必须用固定 bridge 组合环境与 Shopper，并锁 Shopper model/prompt/config。[环境 action parser](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/web_agent_site/engine/engine.py#L86-L101)；[multi shopper](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/multi_eval/shopper.py#L79-L139)（访问日期：2026-08-19）。

### 5.3 生命周期与安全差异

- server 默认创建 20 个环境；没有 lease token、generation、session owner 或锁。没有 `env_idx` 的请求最多轮询 5 次，每次等 5 秒。[pack_api.py](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/shop_env/pack_api.py#L14-L25)；[分配逻辑](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/shop_env/pack_api.py#L86-L107)。
- client HTTP timeout 是 30 秒。server 把异常写入 HTTP 200 的 `result.error`，没有版本化 error code 或 schema。[single env](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/single_eval/env.py#L8-L78)；[server error envelope](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/shop_env/pack_api.py#L109-L113)。
- 购买 terminal 或 history 长度超过 42 时，server 立即把 `env_idx` 放回 free set。single runner 又在 `finally` 调用 `release_one`，因此正常购买会重复释放；multi runner 的关闭路径与 single 不同。[terminal auto-release](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/shop_env/pack_api.py#L101-L107)；[`over` 计算](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/shop_env/shop_agent.py#L133-L145)；[single finally](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/single_eval/agent.py#L280-L306)。
- 协议没有 idempotency key 或 action receipt。购买请求若在服务已执行后断线，client 无法从协议判断 outcome。这是从固定协议字段缺失得出的推论，必须由 live bridge fail closed，不能盲目重试。
- 协议没有 `finish_without_purchase`。history/turn limit 只会得到无购买结束或本地零分记录，不能表达课程要求的安全停止。[multi turn limit](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/multi_eval/agent.py#L481-L529)。
- upstream multi prompt 明确要求在用户告别或轮数耗尽前强制 `click[buy now]`。SES 不能继承该行为。[standard prompt](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/multi_eval/configs/standard/qwen3_8b.yaml#L63-L80)；[persona prompt](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/multi_eval/configs/persona/qwen3_8b.yaml#L60-L81)。
- reset response 暴露 `goal_options`，persona 模式还暴露 `reason_key` 和完整 persona。SES bridge 必须把这些字段留在 trusted/private 边界，不能原样发给 Agent、Creator、Updater 或报告。[reset payload](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/shop_env/shop_agent.py#L34-L48)。

这些差异说明 SES 需要自己的购买授权、one-use action/turn lease、conditional close 和 typed error contract。它们不改变上游 reward，也不能让 SES 声称复现上游 leaderboard。

## 6. Reward 核查

固定 commit 在购买时计算并返回：

- raw `reward`：`r_type * (matched_attributes + matched_options + price_ok) / (attribute_count + option_count + 1)`；
- `reward_detail.r_type`：query/category/title 的 soft type match，未命中时为 `0.5`；
- `reward_detail.r_att`：attribute match fraction；
- `reward_detail.r_option`：option match fraction，无目标 option 时为 `1`；
- `reward_detail.r_price`：是否满足价格上限。

来源：[goal.py type reward](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/web_agent_site/engine/goal.py#L114-L153)；[goal.py total reward](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/web_agent_site/engine/goal.py#L206-L245)；[terminal payload](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/shop_env/web_agent_site/envs/web_agent_text_env.py#L579-L613)（访问日期：2026-08-19）。

论文将 additive 指标称为 `R_loose`，将四个约束分量相乘的指标称为 `R_strict`，并单独报告 binary `R_succ`。[论文 reward 公式](https://arxiv.org/html/2601.18225v1#S2.SS3)；[论文 evaluation settings](https://arxiv.org/html/2601.18225v1#S3.SS1)（访问日期：2026-08-19）。

`get_score.py` 的固定聚合语义为：

- `r_loose = data.reward`；
- detail 存在时，缺失 `r_type/r_att/r_price` 默认为 `0`，缺失 `r_option` 默认为 `1`；
- `r_hard = r_type * r_att * r_option * r_price`；
- `r_success = 1` 仅当四个分量都等于 `1`；
- `right = 1` 仅当 `purchase.asin == goal.asin`；
- detail 完全缺失时，四个分量、hard、success 和 right 都为 `0`。

来源：[get_score.py](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/get_score.py#L55-L98)（访问日期：2026-08-19）。

因此，SES 必须原样保留 raw reward，再由独立 projection 计算 strict/full-success/correct-product；不能把 category diagnostics 或课程 safety grade 写回 raw reward。本文只核对上游公式，没有运行或发布任何 task-level reward。

## 7. 可复现性与 drift 结论

| 锁项 | 状态 | 证据 |
| --- | --- | --- |
| Git commit/tree | `verified` | commit 和 tree 已固定 |
| 协议文件 | `verified` | 关键文件 SHA-256 已记录 |
| 上游服务 protocol version | `unknown` | 代码没有 version/schema 标识 |
| repo data revision | `verified`（字节） | gzip/blob/hash 已记录 |
| repo data ↔ HF data mapping | `unknown` | README 指向 mutable `main`；无 mapping manifest |
| search index revision | `unknown` | index 和构建目录都不在固定 tree |
| 四场景 Shopper bridge | `unknown` | 只有分散脚本；没有统一、版本化 bridge |
| Agent model | `unknown` | alias、Provider、endpoint 未锁；论文/代码命名也未形成 manifest |
| Shopper model/prompt/config | `unknown` | prompt 文件可 hash，但 Provider 和实际 served model 未锁 |
| seed/temperature | `unknown` | Agent 写死 temperature `0.0`，Shopper 调用未传 temperature/seed；服务默认值未知 |
| terminal/release | `verified`（当前代码行为） | 已记录 auto-release 和重复 release；不满足 SES lifecycle |
| live error/disconnect | `unknown` | 没有 typed error、lease 或 idempotency contract |

固定提交的 README quick start 不能形成可执行 lock：它没有补齐数据路径、search index、兼容依赖、Provider endpoint 或可工作的 source 值。[README Quick Start](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/README.md#L72-L151)（访问日期：2026-08-19）。

## 8. 费用核查

SiliconFlow 官方价格页在 2026-08-19 列出：

| 模型 | 输入 | 输出 |
| --- | ---: | ---: |
| `Qwen/Qwen3-235B-A22B-Instruct-2507` | ¥2.50 / M tokens | ¥10.00 / M tokens |
| `deepseek-ai/DeepSeek-V3.2` | ¥2.00 / M tokens | ¥3.00 / M tokens |

来源：[SiliconFlow 官方价格页](https://www2.siliconflow.cn/pricing)（访问日期：2026-08-19）。价格页是动态来源，release manifest 必须同时锁访问日期和实际账单，不能把本文数值永久硬编码。

这些单价不能证明 capstone 总成本。上游配置使用 placeholder URL/key 和未限定 Provider 的模型 alias；Agent/Shopper 只读取 `completion.choices`，保存结果时不写 token usage 或 cost。[single model call](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/single_eval/agent.py#L177-L214)；[single output](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/single_eval/agent.py#L216-L248)；[Shopper call](https://github.com/ShopAgent-Team/ShopSimulator/blob/51bb26012cee31aea7ac26177c5ffe807026ac07/multi_eval/shopper.py#L79-L109)（访问日期：2026-08-19）。

本轮没有付费调用，无法完成 spec 要求的 fresh pair 实测。费用状态为 `unknown`，不能按零处理，也不能声称满足人民币 50 元总预算。

## 9. 未执行的 Phase 0 smoke

本来源核查没有执行以下动作：

- 四场景 reset → search/click/ask → purchase/安全停止 → terminal/reward → close；
- error envelope、超时、并发、环境池、purchase 断线和 conditional release 动态测试；
- 目标模型 fresh pair 的 token、费用和墙钟实测；
- 商品图片下载、截图或公开报告生成。

原因不是凭据缺失本身，而是更早的 release blocker：live 必需资产权利仍为 `unknown`，固定提交也缺少可复现 runtime。根据 fail-closed 规则，维护者无需花费或复制更多上游资产来再次证明 `no_go`。

## 10. 风险登记与解除条件

| 风险 | 严重度 | 解除条件 |
| --- | --- | --- |
| ShopSimulator 代码无明确许可证 | blocker | 权利人发布覆盖固定 revision 的许可证或书面授权，并说明衍生/第三方代码 attribution |
| 数据、task、persona 无明确许可证 | blocker | HF 固定 revision 增加可审计 license/card；明确本地执行、课程使用、摘要、公开报告和再分发范围 |
| 商品文本/图片的下游授权不明 | blocker | 平台或数据权利人确认授权范围包含课程 live 执行与预期公开材料；否则公开材料保持零复制、零截图 |
| 搜索索引缺失且衍生权利不明 | blocker | 发布固定 index/build inputs/checksum，且数据条款明确允许生成和使用衍生索引 |
| repo/HF 数据映射未锁 | high | 发布 source manifest，绑定 repo blob、HF revision、split、转换脚本和语义校验 |
| Provider/model/Shopper 未锁 | high | 锁定 provider、served model revision、prompt/config hash、temperature/seed、usage schema 和价格日期 |
| lifecycle/action 协议不安全 | high | 由外部 bridge 提供 lease/generation、幂等 read/close、outcome reconciliation；SES gateway 独占 action execution |
| 费用未实测 | high | 获得授权后运行最小 fresh pair，保存 provider usage、账单货币、单价日期、总额和墙钟 |

只有全部 blocker 变为 `verified`，再完成四场景真实 smoke、独立 live experiment 和 clean-room 后，维护者才能签署新的 `go`。在此之前：

- 保持 HTTP/live profile 默认关闭；
- fixed-v1 只使用课程原创商品、任务、persona 和 in-memory episode；
- 不复制、改写或截图上游商品、任务、persona、图片和索引；
- 不把 fixed 结果称为 ShopSimulator 用户收益或上游 leaderboard 结果。
