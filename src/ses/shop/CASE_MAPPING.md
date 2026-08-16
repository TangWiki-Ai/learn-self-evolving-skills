# Pinned STATE-Bench case and tool mapping

## Source audit

- Upstream repository: `microsoft/STATE-Bench`
- Pinned commit: `5644b1838d96bc4483da29642d058ecaa6f80f7f`
- Audited source files:
  - `state_bench/domains/customer_support/tasks/2-return_defective_electronics.json`
  - `state_bench/domains/customer_support/task_envs/2-return_defective_electronics.json`
  - `state_bench/domains/customer_support/tools.py`
  - `state_bench/domains/customer_support/environment.py`

SES selects `2-return_defective_electronics`, a `return_item` task. The canonical
typed fixture is `fixtures/return_defective_electronics.json`. It owns the case
ID, fixture ID, source commit, transformation version, task time, prompt, order,
item, product, and customer policy fields. `CaseDefinition` and every fresh
environment derive from that fixture.

The source requires `ITEM-9050` from `ORD-6006` to become `returned`, with a
`$1,299` original-payment refund, no restocking fee, a free return label, and
order status `fully_returned`.

The source lists 11 customer-support tools. This ticket uses only the three that
the selected case needs:

| STATE-Bench tool | Upstream input | SES MCP input | Mapping |
| --- | --- | --- | --- |
| `get_order` | `order_id: string` | unchanged | Reads only `ORD-6006`. |
| `get_policies` | `topic` enum with six topics | `topic: "return"` | The case has no use for the other five topics, so SES does not expose them. |
| `process_return` | `item_id`, five-value `reason`, optional `amount` in whole USD dollars, optional `confirm` | `item_id`, the same five-value `reason`, optional `amount_minor`, optional `confirm` | Keeps the upstream preview-then-confirm sequence and verbatim submitted-amount write. `$1,299` maps to `129900` minor units. Confirm also returns `policy_computed_amount` for State Judge comparison. |

## Controlled adaptations

- All upstream business amounts use whole USD dollars. The fixture adaptation
  multiplies them by `100` and represents every amount as
  `Money(amount_minor, "USD")`. MCP renames confirm input `amount` to
  `amount_minor`. Non-defective percentage calculations can therefore retain
  cents that the upstream whole-dollar implementation truncates; this does not
  alter the selected defective case's gold.
- Upstream task time `2026-06-12T10:00:00` has no offset. The fixture pins its
  interpretation to `2026-06-12T10:00:00Z` so date math and artifacts are stable.
- The upstream environment exposes 11 tools. SES exposes only `get_order`,
  `get_policies`, and `process_return`; `get_policies` accepts only the `return`
  topic because the selected case cannot use the other topics.
- SES adds `additionalProperties: false`, strict JSON types, and non-negative
  `amount_minor` validation. It deliberately does not compare the submitted
  amount with policy gold. Like STATE-Bench, it writes a wrong non-negative
  amount once; the returned `policy_computed_amount` and terminal snapshot make
  that error observable, and the terminal item cannot be retried.
- The fixture projects one order, item, product, and customer and omits unrelated
  records and customer PII. The environment deep-clones this projection for each
  case run instead of importing STATE-Bench at runtime.
- When the evaluator supplies `--artifact-root`, the MCP process atomically writes
  canonical `ShopSnapshot` records only to `shop/before.json` and
  `shop/after.json` under that root. This trusted transport is not an MCP tool and
  never appears in `tools/list`.

The adapter preserves the source case's scoring-relevant successful and
wrong-amount terminal semantics. It does not implement unrelated orders or the
other eight customer-support tools.
