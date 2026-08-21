# Station 3 — Skill Diagnosis

## Insight

Diagnosis binds a Skill-attributed failure to a document location and an action.
Choose one of five labels: `missing`, `incomplete`, `misleading`,
`not_effective`, or `rule_correct`. `rule_correct` means the evidence does not
justify changing the text.

## Command

For every Skill-attributed case, record both fields:

```bash
uv run ses journey station 3 \
  --diagnosis CASE_ID=incomplete \
  --location CASE_ID=SKILL.md:12
```

The path must identify a real line below `.ses/skills/working/`.

## Decision prompt

Show the learner the failing trace beside the cited Skill line. Ask: “Is the
needed behavior absent, partial, actively misleading, present but not effective,
or already correct?” Do not infer a location without opening the file.

## Checkpoint

Continue when `.ses/decisions/station-3-diagnoses.json` covers every case whose
station-2 label begins with `skill:`. Environment and case failures need no Skill
diagnosis.

## Production comparison

Pending Owner review. Do not add unreviewed claims about production clustering or
sampling.

## Further source

- Working source: `.ses/skills/working/SKILL.md`.
