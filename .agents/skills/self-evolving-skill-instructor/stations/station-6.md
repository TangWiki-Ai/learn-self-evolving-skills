# Station 6 — Version Release & Rollback

## Insight

Release changes the accepted version only after the two-door Gate accepts the
candidate. The local timeline makes each transition explicit. The rollback
rehearsal returns to v0, then restores v1 so the learner experiences both
directions without losing the accepted improvement.

## Command

Ask the learner to choose one action:

```bash
uv run ses journey station 6 --action release
uv run ses journey station 6 --action release-rollback-restore
uv run ses journey station 6 --action defer
```

A rejected Gate only permits `defer`.

## Decision prompt

Ask: “Does the Gate evidence justify release, and do you want to rehearse the
rollback now?” Do not release merely to make the dashboard green.

## Checkpoint

Inspect `.ses/evidence/version-timeline.json` and the timeline report. If the
candidate was released, verify `.ses/versions/v1/` hashes to the Gate candidate.

## Production comparison

Pending Owner review. State clearly that this is a local version timeline, not a
production deployment, traffic shift, or live rollback.

## Further source

- Local report: `.ses/reports/station-6-versions.html`.
