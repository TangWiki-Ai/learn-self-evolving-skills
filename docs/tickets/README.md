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

- [ ] Skill installation copies the Skill and references while excluding eval material.
- [ ] Both runs use fresh workspaces and produce distinct traces rather than cached output.
- [ ] A reference Skill provides a deterministic fallback when learner generation is weak.
- [ ] Lesson 1 includes the creator prompt, starter, solution, tests, and qualitative comparison artifact.

## 04 - Run a reproducible baseline and render an L1 report

**Blocked by:** 02 - Grade one return case from terminal state.

**What it delivers:** A learner can run the develop split through a constrained user simulator, resume multi-turn sessions, enforce run budgets, repeat cases, and open a self-contained L1 report covering outcomes, evidence, cost, latency, and variance.

**Acceptance criteria:**

- [ ] Batch runs isolate every case and support safe resume and explicit reruns.
- [ ] Case, turn, token, and cost limits interrupt cleanly while preserving partial traces.
- [ ] The L1 report exposes summary metrics and per-case evidence without leaking hidden task data.
- [ ] Repeated runs report reliability as pass^k or an equivalent variance-aware measure.
- [ ] Lesson 4 starter, solution, tests, and baseline comparison are included.

## 05 - Calibrate evidence-based AI judges

**Blocked by:** 02 - Grade one return case from terminal state.

**What it delivers:** A learner can compare a rubric-based LLM judge with an evidence-based judge agent against a small human-reviewed set and see where each judge should return pass, fail, or not evaluated.

**Acceptance criteria:**

- [ ] Deterministic evidence extraction produces StateDiff, tool timeline, and amount reconciliation facts before any model judgment.
- [ ] Both judge modes return a validated grading record with assertion-level evidence.
- [ ] Calibration compares judge outputs with human labels and records disagreements without claiming an invented accuracy gain.
- [ ] The judge agent receives read-only evidence and cannot mutate the shop environment.
- [ ] Lesson 3 starter, solution, tests, and agreement experiment are included.

## 06 - Mine candidate cases from ABCD and tau2-bench

**Blocked by:** 01 - Prove the runtime and data smoke path.

**What it delivers:** A learner can reproducibly clean and deduplicate the PRD-defined ABCD slice, inspect intent clusters against existing labels, and use tau2-bench trajectories to produce a difficulty-stratified candidate list.

**Acceptance criteria:**

- [ ] Acquisition and slicing preserve source version, license, manifest, checksum, and transformation history.
- [ ] Cleaning preserves the original-to-delexed relationship needed for self-checking.
- [ ] Clustering produces measurable comparisons against flow and subflow labels.
- [ ] tau2-bench data remains read-only and contributes only deduplication and difficulty signals.
- [ ] Lesson 5 starter, solution, tests, and funnel metrics are included.

## 07 - Turn candidates into verified develop cases

**Blocked by:** 02 - Grade one return case from terminal state; 05 - Calibrate evidence-based AI judges; 06 - Mine candidate cases from ABCD and tau2-bench.

**What it delivers:** A learner can generate controlled return-policy variants, calculate gold outcomes with the deterministic shop policy, replay and calibrate each candidate, and add only qualified cases to the develop split.

**Acceptance criteria:**

- [ ] Variant generation changes supported policy dimensions without embedding fixed answers.
- [ ] Gold outcomes come only from deterministic policy execution and match standard-operation replay.
- [ ] Every accepted case passes environment replay, deliberate correct/incorrect judge checks, and recorded human review.
- [ ] Split checks prevent overlap and prohibit writes to locked selection and final sets.
- [ ] Lesson 6 starter, solution, tests, qualification rate, and expanded baseline rerun are included.

## 08 - Create Skill v0 and render a paired L2 comparison

**Blocked by:** 03 - Show the first with/without Skill comparison; 04 - Run a reproducible baseline and render an L1 report; 07 - Turn candidates into verified develop cases.

**What it delivers:** A learner can create Skill v0 from the nine approved creator traces in an isolated workspace, pass static and trigger gates, compare v0 with baseline on develop cases, and inspect a paired L2 report.

**Acceptance criteria:**

- [ ] The Creator can read only approved successful traces and use only an explicit safe tool set.
- [ ] Static checks reject forbidden identifiers, fixed answers, unsupported tools, and excessive content.
- [ ] Trigger evaluation reports precision and recall on ten positive and ten negative prompts.
- [ ] The L2 report pairs fresh baseline and v0 runs and exposes improvements and regressions case by case.
- [ ] Lesson 7 starter, solution, tests, and quantitative comparison are included.

## 09 - Generate an evidence-linked candidate patch

**Blocked by:** 08 - Create Skill v0 and render a paired L2 comparison.

**What it delivers:** A learner can turn failed evaluation evidence into typed failure cards, trace each proposed change back to evidence, and apply a small add, update, or delete patch to produce an immutable candidate Skill version.

**Acceptance criteria:**

- [ ] Failure analysis distinguishes trigger, pattern, overload, terminology, timing, and safety failures.
- [ ] Each patch operation names its evidence and changes only the smallest necessary Skill content.
- [ ] Candidate creation cannot mutate the accepted parent version.
- [ ] Invalid or ungrounded patches fail before live evaluation.
- [ ] Lesson 8 starter, solution, tests, and evidence-linked patch list are included.

## 10 - Gate and govern Skill versions

**Blocked by:** 04 - Run a reproducible baseline and render an L1 report; 08 - Create Skill v0 and render a paired L2 comparison; 09 - Generate an evidence-linked candidate patch.

**What it delivers:** A learner can run trigger and live selection gates, reject regressions or cost blowups, promote a passing candidate, inspect the complete version lineage, and roll back to a previously accepted Skill.

**Acceptance criteria:**

- [ ] Selection tasks remain hidden and locked while candidates receive only aggregate gate results.
- [ ] Ties, regressions, trigger failures, and budget violations reject the candidate with evidence.
- [ ] Registry events are append-only and retain accepted, rejected, promoted, and rolled-back versions.
- [ ] At least one acceptance and one rejection or rollback are reproducible in the course fixture.
- [ ] Lesson 9 starter, solution, tests, and gate decision record are included.

## 11 - Run bounded auto-evolution and export the portfolio

**Blocked by:** 10 - Gate and govern Skill versions.

**What it delivers:** A learner can run at least two bounded rollout-reflect-patch-gate rounds, stop on budget or convergence guards, evaluate the accepted Skill once on the locked final split, and export a self-contained portfolio with an L3 evolution report.

**Acceptance criteria:**

- [ ] The loop enforces round, token, cost, cooldown, and freeze guards and preserves partial progress.
- [ ] Every candidate follows the same registry and gate path as a manually created candidate.
- [ ] Final evaluation hides gold, reference traces, and final answers from all modifying agents.
- [ ] Portfolio output includes version lineage with rejected branches, gate records, final metrics, cost, and an architecture summary.
- [ ] Lesson 10 starter, solution, tests, evolution curve, and final twelve-case report are included.

## 12 - Validate the complete course release

**Blocked by:** 11 - Run bounded auto-evolution and export the portfolio.

**What it delivers:** A maintainer can verify that a fresh learner environment follows all ten lessons in order, every solution becomes the next starter, all required comparison artifacts are reproducible, and the complete course stays within its documented safety and budget constraints.

**Acceptance criteria:**

- [ ] A clean-room run verifies every starter, solution, test suite, command, and lesson transition.
- [ ] Dataset manifests, licenses, checksums, split isolation, and credential redaction checks pass.
- [ ] Reference runs and cost tables clearly separate measured values from estimates.
- [ ] The release checklist proves the PRD completion criteria or records any explicit deviation.
- [ ] Public documentation consistently describes benchmark and role-play data without claiming production provenance.
