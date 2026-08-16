---
name: resolve-product-returns
description: Use for product return requests that require policy checks and safe state changes.
allowed-tools: get_order, get_policies, process_return
---

# Resolve product returns

1. Inspect the order with `get_order`. Identify only the item the customer wants to return.
2. Read the current rules with `get_policies`; do not guess eligibility, fees, or timing.
3. Call `process_return` in preview mode. Explain the tool-produced result and ask for consent.
4. Confirm with the same item and reason only after the customer approves the preview.
5. Verify the returned terminal state. Report only actions and amounts supported by tool evidence.

Do not expose internal terminology, invent a fixed answer, or change unrelated items.
