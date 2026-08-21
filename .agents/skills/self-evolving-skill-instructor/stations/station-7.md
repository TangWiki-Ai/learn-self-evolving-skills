# Station 7 — Summary

## Insight

The final package is deterministic. It fills every number from prior evidence;
no LLM rewrites the result. It always labels the work as a STATE-Bench
customer-support sandbox. Running this station never converts unfinished work
into success.

## Command

```bash
uv run ses journey station 7
```

Run it even if station 5 or 6 needs attention. Rerun after later improvements to
refresh the current facts.

## Decision prompt

There is no required product decision. Ask the learner to verify three numbers
against `.ses/deliverables/evidence-facts.json`, then practice answering the
interview questions with the cited files open.

## Checkpoint

Confirm these files exist:

- `resume-zh.md` and `resume-en.md`
- `interview-prep.md`
- `concepts.md`
- `evidence-facts.json` and `evidence-index.json`

The dashboard should link every deliverable. Report any earlier attention state.

## Production comparison

The generated interview and concept files deliberately mark this section pending
Owner review. Do not fill those placeholders from memory or inference.

## Further source

- Machine source of truth: `.ses/deliverables/evidence-facts.json`.
