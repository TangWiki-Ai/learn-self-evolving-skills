# Contract proposal: Lesson 1 comparison to Runner ComparisonRecord

## Need

Lesson 1 now persists a strict `lesson_1_skill_demo_comparison` record in
`ses.skills.comparison`. It is intentionally qualitative and must not define
the canonical Runner-owned `ComparisonRecord` before the Runner owner lands it.

## Proposed owner and consumers

- Producer and owner: Simulation/Runner.
- Consumers: Reporting, Skill Creation, Course Delivery, Gate, Automation.

## Proposed minimum fields

- `schema_version: Literal["v1alpha1"]`
- `record_type: Literal["comparison_record"]`
- `comparison_id: str`
- `case_id: str`
- `measured: bool`
- `source: {kind, engine, runtime_config_sha256, model_lock_sha256}`
- `protocol: {sha256, same_for_both_runs}`
- `baseline_run: ArtifactRef`
- `candidate_run: ArtifactRef`
- `baseline_skill_sha256: str | None`
- `candidate_skill_sha256: str | None`

The Runner owner should add paired-run compatibility checks and outcome/cost
classification when Issue #8 introduces quantitative L2 reporting.

## Invariants and migration

- Both runs use fresh workspaces and distinct Trace IDs.
- Case and protocol hashes match before comparison.
- `measured=false` records cannot support an improvement claim.
- Artifact references stay relative and checksum-verified.
- No migration is required now: Lesson 1 keeps its distinct record type. When
  the canonical contract lands, Course Delivery can add an explicit adapter
  from this qualitative record instead of changing the meaning of stored data.
