# Station 0 — Execution & Monitoring

## Insight

A baseline is a recorded execution, not a model's self-assessment. In this
repository each case produces a Trace, tool timeline, state diff, deterministic
Judge result and token usage. Cost is shown only with its source: a Claude Code
estimate is not the Provider's final bill, unavailable cost stays unavailable,
and synthetic CI data never represents live cost. The full v0 baseline
runs 15 cases. A separate fixed five-case sample runs without the Skill; always
say `n=5` and never compare it as if both sides used all 15 cases.

## Command

```bash
uv run ses journey station 0 --provider siliconflow
# or
uv run ses journey station 0 --provider chatanywhere
```

The command also runs local doctor checks and initializes the working Skill at
`.ses/skills/working/`. Use exactly one Provider flag for a fresh live workspace.
SiliconFlow requires `SILICONFLOW_API_KEY`; ChatAnywhere requires
`CHATANYWHERE_API_KEY` and the locked Claude-series model. Do not print or persist
the key. On resume, use the Provider already saved in `.ses/status.json`.

## Decision prompt

There is no learner decision in this station. Ask them to observe:

- How many of the 15 v0 cases passed?
- Which failures are agent failures, Judge errors, or infrastructure errors?
- What does the five-case no-Skill sample show, and what can it not prove?

## Checkpoint

Continue when `.ses/evidence/station-0.json` exists and dashboard station 0 is
complete. Open both L1 links. If doctor or the live run fails, use the recorded
reason and resume the same command after fixing it; do not delete `.ses/`.

## Production comparison

Pending Owner review. For now state only: this is a controlled STATE-Bench
customer-support sandbox, not production traffic or production monitoring.

## Further source

- [OpenAI: Build skills](https://learn.chatgpt.com/docs/build-skills) — project
  Skill structure and progressive disclosure.
