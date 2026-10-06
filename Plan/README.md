# Plan index

The plan documents are grouped by delivery status:

- `completed/`: plans with no remaining unchecked or partially completed work.
- `in-progress/`: every plan that is not finished: not started, pending work, partial work, operator acceptance, or blockers.

Legacy paths under `Plan/` remain as symlinks so existing references continue to work.

Architecture-aware regression testing:

- `in-progress/architecture-impact-analysis-regression-plan.md`: detailed plan for Git-diff impact analysis, safe test selection/execution, CI reporting, and optional read-only Stream integration. It elaborates Phase 4 of `regression-test-gate-plan.md`.
