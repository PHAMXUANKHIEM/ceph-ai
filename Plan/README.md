# Plan index

The plan documents are grouped by delivery status:

- `completed/`: plans with no remaining unchecked or partially completed work.
- `in-progress/`: plans with pending work, partial work, operator acceptance, or blockers.
- `incompleted/`: focused implementation plans that are scoped but not yet started or completed.

Legacy paths under `Plan/` remain as symlinks so existing references continue to work.

Architecture-aware regression testing:

- `incompleted/architecture-impact-analysis-regression-plan.md`: detailed plan for Git-diff impact analysis, safe test selection/execution, CI reporting, and optional read-only Stream integration. It elaborates Phase 4 of `regression-test-gate-plan.md`.
