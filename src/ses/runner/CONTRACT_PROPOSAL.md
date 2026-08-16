# Runner contract handoff

Issue #4 now defines the producer-owned contracts in
`src/ses/contracts/runner.py`. Reporting and Runner import these models directly;
this branch intentionally leaves `src/ses/contracts/__init__.py` unchanged so the
integrator can coordinate the final package-level export.

The contract includes:

- `RunnerStatus`, with separate Agent, Simulator, Judge, infrastructure, budget,
  and not-evaluated outcomes.
- `RunConfig`, whose canonical hash covers the data version, model lock hash,
  Skill hash, protocol version, and complete case/iteration plan.
- `BudgetState`, which records independent limits and consumption across every
  append-only attempt.
- `RunRecord` and `RunArtifacts`, which link each attempt to relative,
  content-addressed Trace, snapshot, StateDiff, and CaseGrade artifacts.

The JSONL format is `v1alpha1` and append-only. Retry attempts reuse the case and
iteration slot but receive a new `attempt_id`; reports display the latest attempt
while cost, token, turn, and latency totals include every attempt.
