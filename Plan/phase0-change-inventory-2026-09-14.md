# Phase 0 Change Inventory

Target: `10.3.55.213:/root/ceph-ai`
Branch: `main`
Reviewed against: `f8e2ab979e1e276d3332ad80cf45cb198ad3034c`

This inventory classifies the current dirty worktree. It is a review aid, not
an approval to commit or deploy. Files with application behavior changes must
be split from tests and documentation before release.

## A. Realtime snapshot and dashboard foundation

Application and UI:

- `Plan/realtime-cluster-status-update-plan.md`
- `config/settings.py`
- `shared/ceph_query_cache.py`
- `shared/cluster_snapshot.py`
- `dashboard/cache_warmup.py`
- `dashboard/cluster_scope.py`
- `dashboard/routes/pgs.py`
- `dashboard/routes/nodes.py`
- `dashboard/routes/crush_map.py`
- `dashboard/routes/incidents.py`
- `dashboard/ws.py`
- `watcher/ceph_client.py`
- `watcher/cluster_snapshot_collector.py`
- `watcher/main.py`
- `ceph-health-dashboard/src/components/CephDashboard.tsx`
- `ceph-health-dashboard/src/components/PoolsPage.tsx`
- `ceph-health-dashboard/src/styles.css`
- `dashboard/static/ceph-health/app.js`
- `dashboard/static/ceph-health/style.css`
- `dashboard/static/crush_map.js`
- `dashboard/static/nodes.js`
- `dashboard/static/pgs.js`
- `dashboard/static/style.css`
- `dashboard/templates/crush_map.html`
- `dashboard/templates/nodes.html`
- `dashboard/templates/pgs.html`
- `dashboard/templates/pools.html`

Tests:

- `tests/test_cluster_snapshot.py`
- `tests/test_cluster_snapshot_collector.py`
- `tests/test_dashboard_clusters.py`
- `tests/test_dashboard_crush_map.py`
- `tests/test_dashboard_health_api.py`
- `tests/test_dashboard_ws.py`

Risk: this is the largest behavior slice and must be released as a separate
read-only canary before any mutation or Watcher rollout.

## B. Storage, object storage, navigation, and UI safety

- `dashboard/static/app.js`
- `dashboard/static/chat_widget.js`
- `dashboard/templates/_nav.html`
- `dashboard/templates/index.html`
- `dashboard/templates/bucket_access_log.html`
- `tests/test_dashboard_object_storage.py`

Risk: navigation and permission-gated UI changes need browser smoke tests and
must be kept separate from backend snapshot changes.

## C. Database and schema compatibility

- `alembic/versions/3c4d5e6f7a8b_widen_backup_job_size.py`
- `alembic/versions/ac17d9e0f5a0_widen_log_finding_verdict.py`
- `alembic/versions/b2c3d4e5f6a7_add_volume_mapping_samples.py`

Risk: these changes require an isolated upgrade/downgrade review and a
database backup before deployment.

## D. Incident, log-intelligence, and safety behavior

Application:

- `shared/logging_redaction.py`
- `watcher/ceph_finding_verifier.py`
- `watcher/volume_topology.py`
- `dashboard/routes/upgrade.py`

Tests:

- `tests/test_alert_center.py`
- `tests/test_backup_engine.py`
- `tests/test_ceph_client.py`
- `tests/test_change_risk.py`
- `tests/test_dashboard_actions.py`
- `tests/test_incident_grouping.py`
- `tests/test_log_intelligence_e2e.py`
- `tests/test_models.py`
- `tests/test_node_upgrade_gate.py`
- `tests/test_performance_rca.py`
- `tests/test_pha0_safety_matrix.py`
- `tests/test_telegram_approval_bot.py`
- `tests/test_watcher_incident_flow.py`
- `tests/test_watcher_main.py`

Risk: redaction, deduplication, and approval semantics require security and
rollback review before release.

## E. Worker and autonomous-operation behavior

- `worker/code_repair_supervisor.py`
- `tests/test_code_repair_supervisor.py`

Risk: the runtime currently reports global autonomous flags enabled. Keep this
slice out of a release commit until the operator explicitly reviews the flag
state and the per-cluster safety gate.

## F. New files requiring explicit review

- `Plan/end-to-end-feature-completion-and-deployment-plan.md`
- `Plan/release-manifest-2026-09-14.yaml`
- `tests/test_cache_warmup.py`
- `watcher/inventory_queries.py`

The new Python files are covered by compilation and targeted tests, but they
must be reviewed and included deliberately rather than swept into a broad
commit.

## Phase 0 disposition

- No files were discarded or reset.
- The verified realtime foundation was isolated in commit
  `f8e2ab979e1e276d3332ad80cf45cb198ad3034c`.
- No push or service restart was performed.
- The remaining worktree is not yet suitable for deployment because migration,
  incident/logging, UI navigation, and autonomous-worker changes still need
  separate review and commits.
- Next release action: split groups A–E into reviewed commits, rerun the full
  release gate from the resulting clean candidate, and obtain operator review
  of runtime ownership and autonomous flags.
