# Architecture

```text
fresh develop rollout -> reflect -> bounded patch -> shared Gate -> Registry
          ^                                               |
          |                                               v
          +---------- current accepted Skill <- accept/promote or reject

loop stop -> one-time final aggregate -> L3 report -> allowlisted portfolio
```

The automatic loop composes the same Gate and Registry used by manual candidates.
It cannot change evaluation policy, split locks, Judge logic, or the accepted pointer
without a complete accepted Gate decision. Final runs after the loop and returns only
an aggregate; it never becomes patch input.
