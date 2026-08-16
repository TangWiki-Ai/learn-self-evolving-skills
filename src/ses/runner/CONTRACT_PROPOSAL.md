# Contract proposal: RunRecord and BudgetState

Issue #4 needs canonical runner persistence, but this lane does not own
`src/ses/contracts/**`. The current implementation therefore writes a narrow,
append-only `baseline_run_event` JSONL format and does not publish a duplicate
Pydantic contract.

## Proposed producer-owned contracts

Producer: Simulation/Runner. Consumers: Reporting, Gate, Automation.

`RunRecord` should contain `run_id`, immutable configuration hash, data/model/skill
lineage, ordered case/iteration slots, artifact references, and the latest terminal
status for each slot. `BudgetState` should contain independent case, turn, input
token, output token, and decimal cost limits plus consumed values and one stop
reason.

The shared status enum needs these distinct values: `pass`, `agent_fail`,
`judge_error`, `infrastructure_error`, `budget_stop`, and `not_evaluated`.

## Invariants and migration impact

- Persist events with `schema_version=v1alpha1`, a record discriminator, and a
  monotonically increasing sequence.
- Append state transitions; never replace an earlier iteration result.
- Resume only a matching configuration hash. Explicit reruns allocate a new
  iteration and link the superseded iteration.
- Use decimal strings for cost and relative content-addressed artifact references.
- Reports consume these records read-only and never invoke a Judge.

The contract owner should replace the temporary JSON validation in
`ses.runner.baseline` with the canonical models and add producer-consumer round-trip,
unknown-field, hash, amount, path, and redaction tests. Existing `events.jsonl`
artifacts need a `v1alpha1` loader or a one-time explicit migration if field names
change.
