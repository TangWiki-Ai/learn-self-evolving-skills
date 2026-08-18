# 开发任务与依赖图

[GitHub Issues](https://github.com/TangWiki-Ai/learn-self-evolving-skills/issues) 是任务状态的唯一来源。本文保留初始拆解和依赖关系，帮助开发者快速理解整体路线。

```text
01 Phase 0 smoke
 |\
 | +----------------------> 06 Mine test candidates
 v
02 Single-case evaluation spine
 |\ \ \
 | |  \ +-----------------> 05 Calibrate AI judges
 | |   \------------------> 04 Batch baseline + L1 report
 | +----------------------> 03 First Skill comparison
 |
02 + 05 + 06 -------------> 07 Verify generated cases
03 + 04 + 07 -------------> 08 Create Skill v0 + L2 report
08 -----------------------> 09 Generate candidate patch
04 + 08 + 09 -------------> 10 Gate and version registry
10 -----------------------> 11 Auto-evolve + final + portfolio
11 -----------------------> 12 Validate course release
```

## 01 - Prove the runtime and data smoke path

**Blocked by:** None - can start immediately.

**What it delivers:** A fast pass/fail check proves that the pinned data sources are reachable and readable, Claude Code headless can call the configured SiliconFlow model, one MCP tool call succeeds, and stream-json can be parsed without persisting credentials.

**Acceptance criteria:**

- [ ] One documented command completes the smoke path within a half-day implementation scope.
- [ ] The check covers dataset access, one model response, one MCP call, and stream-json parsing.
- [ ] Missing prerequisites fail with actionable messages, and all outputs redact credentials.
- [ ] The documentation records that additional Providers are a future extension, not Phase 0 work.

## 02 - Grade one return case from terminal state

**Blocked by:** 01 - Prove the runtime and data smoke path.

**What it delivers:** A learner can run one pinned STATE-Bench return case through an isolated shop environment and MCP tools, capture a normalized Trace and StateDiff, apply deterministic state and rule judges, and inspect a minimal L1 result from the CLI.

**Acceptance criteria:**

- [x] A single CLI command runs one case in a fresh workspace and emits a stable run identifier.
- [x] The result contains messages, tool inputs and outputs, StateDiff, judge assertions, tokens, cost fields, and Skill hash fields where applicable.
- [x] State and rule judges grade observable outcomes and tool order, including failure precedence.
- [x] CLI-level tests run the complete path with a fake engine and no network access.
- [x] Lesson 2 starter, solution, tests, and baseline state pass-rate exercise are included.

## 03 - Show the first with/without Skill comparison

**Blocked by:** 02 - Grade one return case from terminal state.

**What it delivers:** A learner can generate or select a safe demo Skill, install only its allowed files, run the same return case without and with the Skill, and compare the two fresh conversations side by side.

**Acceptance criteria:**

- [x] Skill installation copies the Skill and references while excluding eval material.
- [x] Both runs use fresh workspaces and produce distinct traces rather than cached output.
- [x] A reference Skill provides a deterministic fallback when learner generation is weak.
- [x] Lesson 1 includes the creator prompt, starter, solution, tests, and qualitative comparison artifact.

## 04 - Run a reproducible baseline and render an L1 report

**Blocked by:** 02 - Grade one return case from terminal state.

**What it delivers:** A learner can run the develop split through a constrained user simulator, resume multi-turn sessions, enforce run budgets, repeat cases, and open a self-contained L1 report covering outcomes, evidence, cost, latency, and variance.

**Acceptance criteria:**

- [x] Batch runs isolate every case and support safe resume and explicit reruns.
- [x] Case, turn, token, and cost limits interrupt cleanly while preserving partial traces.
- [x] The L1 report exposes summary metrics and per-case evidence without leaking hidden task data.
- [x] Repeated runs report reliability as pass^k or an equivalent variance-aware measure.
- [x] Lesson 4 starter, solution, tests, and baseline comparison are included.

## 05 - Calibrate evidence-based AI judges

**Blocked by:** 02 - Grade one return case from terminal state.

**What it delivers:** A learner can compare a rubric-based LLM judge with an evidence-based judge agent against a small course-authored reference set that still awaits independent human signature, and see where each judge should return pass, fail, or not evaluated.

**Acceptance criteria:**

- [x] Deterministic evidence extraction produces StateDiff, tool timeline, and amount reconciliation facts before any model judgment.
- [x] Both judge modes return a validated grading record with assertion-level evidence.
- [ ] Calibration compares judge outputs with human labels and records disagreements without claiming an invented accuracy gain.
- [x] The judge agent receives read-only evidence and cannot mutate the shop environment.
- [x] Lesson 3 starter, solution, tests, and agreement experiment are included.

第三项等待独立人工签署。当前课程作者参考标签只支持 fixed/offline 演示，不能称为
`human_reviewed`，也不能作为 live/release calibration 结论。

## 06 - Mine candidate cases from ABCD and tau2-bench

**Blocked by:** 01 - Prove the runtime and data smoke path.

**What it delivers:** A learner can reproducibly clean and deduplicate the PRD-defined ABCD slice, inspect intent clusters against existing labels, and use tau2-bench trajectories to produce a difficulty-stratified candidate list.

**Acceptance criteria:**

- [x] Acquisition and slicing preserve source version, license, manifest, checksum, and transformation history.
- [x] Cleaning preserves the original-to-delexed relationship needed for self-checking.
- [x] Clustering produces measurable comparisons against flow and subflow labels.
- [x] tau2-bench data remains read-only and contributes only deduplication and difficulty signals.
- [x] Lesson 5 starter, solution, tests, and funnel metrics are included.

## 07 - Turn candidates into verified develop cases

**Blocked by:** 02 - Grade one return case from terminal state; 05 - Calibrate evidence-based AI judges; 06 - Mine candidate cases from ABCD and tau2-bench.

**What it delivers:** A learner can use an LLM to triage source evidence and draft semantic rubrics, generate controlled return-policy variants, calculate gold outcomes with the deterministic shop policy, replay and calibrate each candidate, and build a fixed/offline course catalog that remains pending until an independent human signs it.

**Acceptance criteria:**

- [x] Fixed and explicit live curation modes share one strict JSON schema, evidence-binding check, and deterministic environment gate.
- [x] The LLM can propose wording and semantic rubrics but cannot supply amounts, terminal state, oracle data, or its own approval.
- [x] Variant generation changes supported policy dimensions without embedding fixed answers.
- [x] Gold outcomes come only from deterministic policy execution and match standard-operation replay.
- [ ] Every live/release accepted case passes environment replay, deliberate correct/incorrect judge checks, and independently signed human review; unsigned course attestations remain fixed/offline only.
- [x] Split checks prevent overlap and prohibit writes to locked selection and final sets.
- [x] Lesson 6 starter, solution, tests, qualification rate, and expanded baseline rerun are included.

四维 overlap 由受信的外部 inventory verifier 在持久化前检查，且不返回 holdout 身份。
live/release 缺 verifier 时关闭；默认 fixed/offline 只标记 `fixed_offline_unverified`。本轮现有
四个 split 的完整互斥由 release validator 注入外部 bundle 后另行验证。

## 08 - Create Skill v0 and render a paired L2 comparison

**Blocked by:** 03 - Show the first with/without Skill comparison; 04 - Run a reproducible baseline and render an L1 report; 07 - Turn candidates into verified develop cases.

**What it delivers:** A learner can create a fixed/offline Skill v0 from nine course-authored creator traces that passed automated checks but still await direct human review, then pass static and trigger gates, compare v0 with baseline on develop cases, and inspect a paired L2 report. Live or release use remains blocked until an independent reviewer signs the seeds.

**Acceptance criteria:**

- [x] The Creator can read only the trusted loader's fixed/offline pending-review seeds, or independently signed seeds in live/release mode, and use only an explicit safe tool set.
- [x] Static checks reject forbidden identifiers, fixed answers, unsupported tools, and excessive content.
- [x] Trigger evaluation reports precision and recall on ten positive and ten negative prompts.
- [x] The L2 report pairs fresh baseline and v0 runs and exposes improvements and regressions case by case.
- [x] Lesson 7 starter, solution, tests, and quantitative comparison are included.

当前只有 fixed/offline pending-review seeds；live/release Creator 会在 Provider 调用前关闭。

## 09 - Generate an evidence-linked candidate patch

**Blocked by:** 08 - Create Skill v0 and render a paired L2 comparison.

**What it delivers:** A learner can turn failed evaluation evidence into typed failure cards, trace each proposed change back to evidence, and apply a small add, update, or delete patch to produce an immutable candidate Skill version.

**Acceptance criteria:**

- [x] Failure analysis distinguishes trigger, pattern, overload, terminology, timing, and safety failures.
- [x] Each patch operation names its evidence and changes only the smallest necessary Skill content.
- [x] Candidate creation cannot mutate the accepted parent version.
- [x] Invalid or ungrounded patches fail before live evaluation.
- [x] Lesson 8 starter, solution, tests, and evidence-linked patch list are included.

## 10 - Gate and govern Skill versions

**Blocked by:** 04 - Run a reproducible baseline and render an L1 report; 08 - Create Skill v0 and render a paired L2 comparison; 09 - Generate an evidence-linked candidate patch.

**What it delivers:** A learner can run the fixed/offline trigger and selection Gate, reject regressions or cost blowups, promote a passing candidate, inspect the complete version lineage, and roll back to a previously accepted Skill. The canonical live selection runner is a documented release deviation and fails closed when unavailable.

**Acceptance criteria:**

- [x] Selection tasks remain hidden and locked while candidates receive only aggregate gate results.
- [x] Ties, regressions, trigger failures, and budget violations reject the candidate with evidence.
- [x] Registry events are append-only and retain accepted, rejected, promoted, and rolled-back versions.
- [x] At least one acceptance and one rejection or rollback are reproducible in the course fixture.
- [x] Lesson 9 starter, solution, tests, and gate decision record are included.

这些勾选只证明 fixed/offline 路径。canonical live selection runner 尚未实现，Issue #10
仍保持打开。

锁定的 return 候选池排除 creator 和已占用语义组后只有 19 个 eligible group，
selection + final 使用其中 18 个。protected mapping、eligible membership、精确排名、split、
逐题身份和 gold 都不公开；但上游 33-task return source universe 本身公开且很小，18/19 使用
比例也过高。因此这里不声称强抗污染 secrecy。扩大 source pool 或加入经过验证的 keyed policy
后，才能收紧这项 deviation。

## 11 - Run bounded auto-evolution and export the portfolio

**Blocked by:** 10 - Gate and govern Skill versions.

**What it delivers:** A learner can run at least two bounded rollout-reflect-patch-gate rounds, stop on budget or convergence guards, evaluate the accepted Skill once on the locked final split, and export a self-contained portfolio with an L3 evolution report.

**Acceptance criteria:**

- [x] The loop enforces round, token, cost, cooldown, and freeze guards and preserves partial progress.
- [x] Every candidate follows the same registry and gate path as a manually created candidate.
- [x] Final evaluation hides gold, reference traces, and final answers from all modifying agents.
- [x] Portfolio output includes version lineage with rejected branches, gate records, final metrics, cost, and an architecture summary.
- [x] Lesson 10 starter, solution, tests, evolution curve, and final twelve-case report are included.

这些勾选只证明 fixed/offline reference。canonical live auto-evolve 和 final runner 尚未实现，
Issue #11 仍保持打开。

## 12 - Validate the complete course release

**Blocked by:** 11 - Run bounded auto-evolution and export the portfolio.

**What it delivers:** A maintainer can verify that a fresh learner environment follows all ten lessons in order, every solution becomes the next starter, all required comparison artifacts are reproducible, and the complete course stays within its documented safety and budget constraints.

**Acceptance criteria:**

- [ ] A clean-room run verifies every starter, solution, test suite, command, and lesson transition.
- [x] Dataset manifests, licenses, checksums, split isolation, and credential redaction checks pass.
- [x] Reference runs and cost tables clearly separate measured values from estimates.
- [x] The release checklist proves the PRD completion criteria or records any explicit deviation.
- [x] Public documentation consistently describes benchmark and role-play data without claiming production provenance.

第一项只差九组 solution → 下一课 starter 的统一机械 transition manifest；clean-room 会运行
全部文档命令和课程测试，但不会把目录差异冒充继承证明。
