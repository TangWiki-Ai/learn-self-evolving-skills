# Station 4 — Minimal Refinement

## Insight

The learner edits Skill text, not Python. The station snapshots only the runtime
files declared by the v0 manifest, runs the static Gate, hashes the candidate,
and renders a unified diff. A small diff makes the later result easier to
attribute, but this approved product spec does not impose an arbitrary line cap.

## Command

First edit `.ses/skills/working/SKILL.md` (or let the instructor edit it at the
learner's request). Then record the rationale:

```bash
uv run ses journey station 4 --rationale "WHY THIS CHANGE FITS THE DIAGNOSIS"
```

## Decision prompt

Ask the learner to phrase the smallest conditional behavior that addresses the
selected evidence. Check that it does not encode one order ID, one amount, or a
case-specific answer. If every diagnosis is `rule_correct`, changing nothing is
a valid decision.

## Checkpoint

Continue when the static Gate passes and `.ses/current-candidate.json` points to
an immutable candidate. Inspect `.ses/reports/station-4-diff.html` together.

## Further source

- Candidate evidence: `.ses/decisions/station-4-patch-*.json`.
