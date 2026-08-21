---
name: resolve-product-returns
description: Use for product return requests that require policy checks and safe state changes.
allowed-tools: mcp__shop__get_order, mcp__shop__get_policies, mcp__shop__process_return
---

# Resolve product returns

1. Identify the requested item and state the request in customer-facing language before acting.
2. Inspect the order with `get_order`; select only that item.
3. Read the current rules with `get_policies`; do not guess eligibility, fees, or timing.
4. Call `process_return` in preview mode. Explain the tool-produced result and ask for consent.
5. Confirm with the same item and reason only after the customer approves the preview.
6. Verify the returned terminal state. Report only actions and amounts supported by tool evidence.

Do not expose internal terminology, invent a fixed answer, or change unrelated items.
