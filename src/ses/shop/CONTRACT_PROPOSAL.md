# CONTRACT_PROPOSAL: persisted PolicyDecision

## Gap

`docs/specs/10-cross-module-contracts.md` names `PolicyDecision` as a Shop-owned
contract, but `src/ses/contracts/shop.py` does not define or export it.

## Proposed canonical record

The Shop contract owner should add a frozen, versioned `PolicyDecision` record
with these minimum fields:

| Field | Type | Invariant |
| --- | --- | --- |
| `decision_id` | opaque ID | Unique in one case execution. |
| `case_id` | `CaseId` | Identifies the executable case. |
| `policy_version` | non-empty string | Identifies exact policy semantics. |
| `operation` | non-empty string | For this case: `process_return`. |
| `eligible` | strict bool | Indicates whether mutation is allowed. |
| `amount` | `Money \| None` | Uses minor units; required when eligible. |
| `reason_code` | non-empty string | Stable result/rejection code. |

Producer: Shop Environment. Consumers: Evaluation, Testset, Reports.

## Current non-blocking implementation

This lane keeps its return-policy decision as an internal frozen dataclass and
publishes the observable result through the existing `ToolResult` and
`ShopSnapshot` contracts. It intentionally does not create a shadow Pydantic
contract or modify `src/ses/contracts/**`. The integration owner can adopt the
canonical `PolicyDecision` once the contract owner accepts this proposal.
