# Station 5 — Regression Evaluation

## Insight

This Gate has two explicit doors. Door 1 asks whether every selected
Skill-attributed target now passes. Only then does Door 2 rerun all 15 cases and
require zero `pass-to-fail` regressions among cases that v0 passed. Net score is
not a substitute for zero regression.

## Command

Record how the learner intends to react to the Gate:

```bash
uv run ses journey station 5 --decision follow-gate
```

Other choices are `refine` and `hold`. A rejected Gate returns exit code `2` and
keeps all evidence. Go back to station 4 with a new, narrower edit if warranted.

## Decision prompt

Before showing the overall outcome, ask the learner to identify target fixes,
both-pass rows, and red pass-to-fail rows. Then ask whether the wording should be
narrowed, held, or accepted according to the Gate.

## Checkpoint

Open `.ses/reports/station-5-gate.html`. A releasable candidate needs both doors
green. A rejected Gate may remain `needs_attention`; station 7 is still allowed.

## Further source

- Machine Gate record: `.ses/evidence/gate-*.json`.
