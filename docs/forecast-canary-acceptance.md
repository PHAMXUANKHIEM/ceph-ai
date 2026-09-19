# Forecast canary acceptance

The Dashboard endpoint below is read-only and uses the selected cluster as
the canary scope:

```bash
curl -b dashboard-cookie http://127.0.0.1:8000/api/ai-learning/canary
```

The report checks the registered `SHADOW`/`CANDIDATE` models against the
current `ACTIVE` model and returns:

- evaluation count, MAE, SMAPE and false-positive-rate comparison;
- promotion-gate checks and the reason when a gate is blocked;
- raw forecast count, data-quality rate and alert volume;
- explicit `operator_approval_required=true`, `auto_promotion=false`, and
  `remediation_executed=false` markers.

Before accepting a canary, the operator must verify:

1. The report covers only the deliberately selected cluster/host scopes.
2. At least one complete forecast cycle has been evaluated and no stale-data
   quality gate is being ignored.
3. MAE/SMAPE and false-positive-rate gates pass for the configured streak;
   missing precision/recall or early-detection labels remain a blocker.
4. Watcher restart preserves the model registry/evaluation rows. Worker and
   Watcher remain in `AUDIT_ONLY`/`SHADOW_ONLY` unless an operator explicitly
   approves a promotion.
5. A database backup and restore drill has succeeded before any migration.

Promotion is still a separate explicit operator action. Rollback must use the
recorded previous active model and must produce a promotion audit entry.
