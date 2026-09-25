# Archived plan aliases

The repository previously exposed several plans at both `Plan/` and a
delivery-status directory. The root entries are compatibility symlinks, not
independent copies; removing them would break existing references.

Regular plans remain under:

- `Plan/in-progress/` for plans with open work or acceptance gates.
- `Plan/completed/` only after all work and acceptance requirements are closed.
- `Plan/incompleted/` for the canonical strict production-readiness closure
  plan.

Exact duplicate content was checked on 2026-09-25. No regular duplicate was
archived because the remaining same-name pairs are either compatibility
symlinks or have different content. The old staging source trees are archived
outside the repository under the controlled repository archive described in
the production release runbook.
