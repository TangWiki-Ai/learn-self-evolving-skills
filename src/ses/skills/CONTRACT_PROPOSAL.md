# Ticket 08 contract decision

Ticket 08 lands the Runner-owned canonical paired record in
`ses.contracts.runner`:

- `PairCategory`
- `PairedCaseResult`
- `PairedComparison`

Skill Creation produces the candidate and initiates the comparison, but it does
not own a second comparison schema. Reporting and CLI import the canonical
Runner record directly.

The record covers the exact develop case set, protocol hash, fresh-run flags,
four pair categories, scores, token/cost/latency totals, and relative evidence
references. It rejects unknown fields, non-fresh records, incompatible records,
duplicate cases, and category totals that do not match case rows.

No compatibility alias or Ticket 09 gate/registry value was added. Lesson 1
keeps its intentionally distinct qualitative demo record because it makes no
quantitative paired-run claim.
