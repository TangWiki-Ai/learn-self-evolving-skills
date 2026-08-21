# Station 1 — Bad Case Mining

## Insight

Mining turns a run into a bounded worklist. A case belongs here only because the
station-0 evidence says it did not pass. The learner chooses what to investigate;
the command rejects IDs that are not baseline failures.

## Command

First render the list:

```bash
uv run ses journey station 1
```

After the learner chooses, record the decision in the same station command:

```bash
uv run ses journey station 1 --select CASE_ID --select CASE_ID
```

If there are no failures, record that fact with `--select none`.

## Decision prompt

Ask: “Which failures are worth investigating today, and what evidence made you
choose them?” Do not select every row automatically. If asked to decide, prefer a
small set with clear traces and say why.

## Checkpoint

Continue when `.ses/decisions/station-1-selection.json` exists. Verify every
selected ID appears in `.ses/evidence/bad-cases.json`.

## Production comparison

Pending Owner review. Do not turn the sandbox failure list into a claim about how
production bad cases are collected.

## Further source

- Repository evidence contract: `.ses/evidence/bad-cases.json` links each listed
  case back to the station-0 run.
