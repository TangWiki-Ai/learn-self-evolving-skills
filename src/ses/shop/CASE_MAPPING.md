# Pinned STATE-Bench case and tool mapping

## Source audit

- Upstream repository: `microsoft/STATE-Bench`
- Pinned commit: `5644b1838d96bc4483da29642d058ecaa6f80f7f`
- Audited source files:
  - `state_bench/domains/customer_support/tasks/2-return_defective_electronics.json`
  - `state_bench/domains/customer_support/task_envs/2-return_defective_electronics.json`
  - `state_bench/domains/customer_support/tools.py`
  - `state_bench/domains/customer_support/environment.py`

SES selects `2-return_defective_electronics`, a `return_item` task. The source
requires `ITEM-9050` from `ORD-6006` to become `returned`, with a `$1,299`
original-payment refund, no restocking fee, a free return label, and order status
`fully_returned`.

The source lists 11 customer-support tools. This ticket uses only the three that
the selected case needs:

| STATE-Bench tool | Upstream input | SES MCP input | Mapping |
| --- | --- | --- | --- |
| `get_order` | `order_id: string` | unchanged | Reads only `ORD-6006`. |
| `get_policies` | `topic` enum with six topics | `topic: "return"` | The case has no use for the other five topics, so SES does not expose them. |
| `process_return` | `item_id`, `reason`, optional `amount` in whole USD dollars, optional `confirm` | `item_id`, `reason: "defective"`, optional `amount_minor`, optional `confirm` | Keeps the upstream preview-then-confirm sequence. `amount_minor` is USD minor units, so `$1,299` maps to `129900`. SES validates the computed amount before writing, rather than accepting an incorrect amount for a later judge failure. |

The adapter preserves the source case's observable successful terminal state. It
does not import STATE-Bench at runtime, expose its unrelated tools, copy customer
PII, or support unrelated return reasons and orders.
