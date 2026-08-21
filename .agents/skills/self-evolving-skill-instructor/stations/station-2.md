# Station 2 — Failure Analysis

## Insight

Attribution decides whether a Skill edit is even justified. Start with the owner
of the failure: environment, case, or Skill. Only Skill failures get one of four
subtypes: knowledge, tool, clarification, or style. This station records the
learner's judgment; it does not call a model to overwrite it.

## Command

Provide one assignment for every selected case:

```bash
uv run ses journey station 2 \
  --attribution CASE_ID=environment \
  --attribution CASE_ID=case \
  --attribution CASE_ID=skill:knowledge
```

Allowed Skill subtypes are `knowledge`, `tool`, `clarification`, and `style`.
With an empty station-1 selection, run the base command without assignments.

## Decision prompt

For each trace ask: “What concrete evidence makes this an environment problem,
a faulty case, or a Skill problem?” Require a sentence before recording a label.

## Checkpoint

Continue when every selected case has exactly one label in
`.ses/decisions/station-2-attributions.json`. Use the dashboard distribution only
as a summary; the per-case decision file is authoritative.

## Production comparison

Pending Owner review. Say only that this human attribution happened on sandbox
evidence and should not be generalized to a production taxonomy yet.

## Further source

- Local report: `.ses/reports/station-2-attributions.html`.
