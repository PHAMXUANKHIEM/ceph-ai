# Release Stability and AI Evidence Follow-up — 2026-09-24

**Repository:** `PHAMXUANKHIEM/ceph-ai`
**Review baseline:** `7abd88ca7f98344bb588b6a5f870f3f2369bf6c3`
**Review source:** Independent review supplied on 2026-09-24
**CI run:** `35822665843`
**Status:** `[~]` Open — release is blocked until CI, integration and runtime
evidence gates pass.

This document converts the latest review into executable follow-up work. It is
not a release approval and does not turn benchmark results into production
evidence. The existing `Single Full` mode remains a separate, fully authorized
operator mode; this plan does not narrow or route it through the typed
Incident/Autopilot executor.

## 1. Immediate operating decision

- [x] Keep `online_learning_enabled=false`, `online_learning_mode=AUDIT_ONLY`,
  `river_linear_v2=false` and `snarimax_shadow_enabled=false` until verified
  outcomes and canary evidence exist.
- [x] Keep production in `ADVISORY` or `APPROVAL_REQUIRED`; do not enable
  `LIMITED_AUTOPILOT` from this review alone.
- [x] Treat the current HEAD as **not release-ready** while Python 3.11,
  Python 3.12 or integration jobs are red.
- [x] Treat the 0-verified-outcome River report as insufficient evidence, not
  as a model failure or a reason to weaken the gate.
- [ ] Do not run destructive or prompt-injection boundary tests against a real
  Ceph cluster; use fixtures, a sandbox or isolated staging.

## 2. Release gate — fix CI first (P0)

### 2.1 Compose/resource-policy contract

- [x] Update `test_compose_resource_caps_match_policy` to reflect the new
  architecture: `code-repair` is no longer a production Compose service and is
  owned by `ceph-ai-code-repair-supervisor.service`.
- [x] Assert the six runtime services have resource caps and that the Compose
  file has no source bind mount; test the supervisor separately for its
  push/deploy/promotion-disabled environment.
- [x] Do not re-add `code-repair` merely to make the old test pass.

**Acceptance:**

```text
.venv/bin/pytest -q tests/test_executor_isolation.py tests/test_production_packaging.py
```

Expected: no `KeyError: 'code-repair'`; test names explain the runtime-owner
boundary.

### 2.2 Action policy/contract registry

- [x] Reconcile `rbd_copy_volume` across
  `worker/policy/action_policy.yaml`, typed action contracts and
  `tests/test_action_contract.py`.
- [x] Decide and document its classification, typed parameters, capability,
  target type, idempotency behavior, expiry, preflight and post-check.
- [x] If the action is not ready for the executor, remove it from the policy
  surface rather than weakening the contract test.
- [ ] Add a regression test that policy action IDs and contract action IDs have
  an intentional one-to-one relationship, with an explicit allowlist for
  advisory-only or legacy entries.

**Acceptance:**

```text
.venv/bin/pytest -q tests/test_action_contract.py tests/test_policy_gate.py
```

No `Extra item: rbd_copy_volume` and no untyped mutation may enter the
Incident/Autopilot path.

**Implementation evidence (2026-09-24):** Removed the stale `code-repair`
entry from `shared/resource_limits.py` because the tool is host-supervised,
not a Compose service; updated the reserve assertion for the resulting 5.75
CPU runtime budget. Confirmed that `rbd_copy_volume` remains deliberately
outside the management/Incident enum and is guarded by the Dashboard-specific
typed contract, rather than weakening the policy gate to accept it. Focused
verification passed: `42 passed` for resource/policy tests, `295 passed` for
command, RBD reconciliation and volume-dashboard tests, and `7 passed` for
executor-isolation/production-packaging tests. The full CI and integration
gates remain open; the one-to-one policy/contract regression test is still
pending.

### 2.3 Integration failure diagnosis and repair

- [ ] Download the complete integration JUnit/log artifact for CI run
  `35822665843`; do not infer the cause from the public annotation alone.
- [ ] Reproduce locally with an isolated RabbitMQ instance and the exact CI
  environment variables, Python version and dependency lock.
- [ ] Fix the underlying MQ/DB contract, not only the assertion or timeout.
- [ ] Add a deterministic fixture for the failure and preserve a separate
  live-chaos test marker.

**Acceptance:**

```text
pytest -m integration --junitxml=artifacts/pytest-integration.xml
pytest -q
```

Both Python 3.11 and Python 3.12 jobs must pass; `quality`, `release_gate` and
`deploy` must become runnable again.

### 2.4 Clean-runner release evidence

- [ ] Confirm `release_gate` installs the same locked application dependencies
  used by the image build.
- [ ] Confirm the quality artifact contains test JUnit, pip-audit, image scan,
  SBOM, registry image reference and release manifest.
- [ ] Verify that a failed test, integration job, scan or artifact download
  blocks deployment.

**Acceptance:** one clean GitHub Actions run completes through `release_gate`
without manual edits or host-local dependencies.

## 3. River v2 and verified-outcome evidence (P1)

### 3.1 Make the zero-outcome state explicit

- [ ] Add a report field distinguishing `NO_VERIFIED_OUTCOME` from
  `INSUFFICIENT_SAMPLE`, `NO_DATA` and `MODEL_NOT_RUN`.
- [ ] Show, per scope: verified outcome count, scored outcome count, latest
  evidence time, active/shadow model, data-quality gaps and reason promotion is
  blocked.
- [ ] Keep `river_linear_v2` shadow-only when verified outcome count is zero.

### 3.2 Collect verified outcomes without label leakage

Implementation note (2026-09-24): the numeric River v2 label path is
`Loki CPU/RAM observation -> EVALUATED NodeResourceForecastRun -> matching
unlabeled OnlineLearnerAudit -> OnlineLearnerLabel -> shadow replay`.
The label is independent telemetry, not an operator verdict or a post-check
decision. Newly accepted labels require matching observed values/timestamps,
valid evaluation ordering and a recent evaluated run; they carry the source
run, cluster/host/metric, observation time, baseline model identity and an
evidence fingerprint. Incident/action IDs are nullable and remain unset for
this telemetry-only path. Existing labels without provenance are excluded from
River v2 runtime and replay. No historical RESOLVED incident is backfilled.
Tests cover accepted, duplicate, missing audit, mismatched/self-labeled and
stale outcomes. This remains partial: independently collected live labels and
the operator/post-check categorical-outcome path have not been demonstrated.

- [ ] Trace the full path from operator verdict/post-check to the River training
  dataset and prove that labels do not come from the model's own prediction or
  an unverified alert.
- [ ] Store source incident/action ID, cluster/scope, outcome timestamp,
  model/prompt version and evidence fingerprint with every accepted label.
- [ ] Reject duplicate, stale, synthetic or unverified outcomes according to
  the existing policy; do not manufacture labels from historical `RESOLVED`
  rows.
- [ ] Add isolated tests for accepted, duplicate, missing-evidence and
  model-self-labeled outcomes.

### 3.3 Replay and baseline comparison

- [ ] Replay `river_linear_v2` on a temporal holdout separated by cluster and
  scope; compare it against the active baseline and a simple naive baseline.
- [ ] Report MAE/RMSE or the approved metric, coverage, alert volume, abstention,
  false-positive rate and data-quality failure rate per scope.
- [ ] Do not promote on aggregate score alone; a candidate must not regress a
  protected scope or health code.
- [ ] Keep the promotion decision operator-approved and auditable.

**Acceptance:** a quality report contains non-zero, independently verified
outcomes, temporal holdout boundaries, baseline comparison and a clear
`PROMOTE`/`HOLD` reason. Until then, status remains `[~]` and `SHADOW_ONLY`.

## 4. Drift and anomaly candidates (P1)

### 4.1 Investigate the 21 drifted scopes

- [ ] Export the 21 scope IDs and classify each cause: workload change,
  collector gap, timestamp issue, counter reset, true distribution change or
  insufficient history.
- [ ] Add evidence links and remediation owner for each classification.
- [ ] Keep affected scopes in `HOLD`; do not reset drift state or lower thresholds
  solely to improve the dashboard.
- [ ] Re-run the walk-forward evaluation after correcting only confirmed data
  quality issues; preserve before/after reports.

### 4.2 HST and RRCF decision gate

- [ ] Keep HST shadow-only while its fixture benchmark has 47.83% false-positive
  rate and 8.33% precision.
- [ ] Keep RRCF shadow-only despite better fixture results until the same
  time-split, cluster-separated benchmark is run on a larger redacted dataset.
- [ ] Add alert-volume and abstention controls to the candidate report, not only
  event recall and precision.
- [ ] Define the canary threshold and protected-scope regression rule before
  opening any candidate.

**Acceptance:** no candidate is promoted because of the 72-point fixture alone;
the report includes dataset size, split method, confidence/uncertainty and
operator decision.

## 5. Production recovery and security evidence (P1)

### 5.1 PostgreSQL migration, restore and rollback rehearsal

- [ ] Restore a production-like PostgreSQL backup into isolated staging.
- [ ] Upgrade from the current migration head using the exact image digest.
- [ ] Simulate image failure before and after migration; record recovery steps,
  time, backup artifact, migration revision and schema compatibility.
- [ ] Test container rollback separately from database rollback. Never assume an
  application image rollback reverses a schema migration.
- [ ] Obtain operator witness and attach the rehearsal artifact to the release
  manifest.

### 5.2 Incident Outbox chaos

- [ ] Test broker failure immediately after Incident commit.
- [ ] Kill publisher before and after RabbitMQ publisher confirmation.
- [ ] Kill consumer during claim, processing and completion.
- [ ] Verify retry/backoff, DLQ/reconciler behavior, idempotency and no duplicate
  mutation or Telegram notification.
- [ ] Record queue age, attempts, recovery time and final Incident state.

### 5.3 RBAC and cross-cluster execution

- [ ] Build an action/capability/route matrix for read, preview, execute, admin
  and destructive operations.
- [ ] Test direct API, WebSocket, Worker and Dashboard paths with a user lacking
  the capability.
- [ ] Test changing cluster selection while holding stale evidence or an old
  target; the server must reject cross-cluster execution.
- [ ] Verify the audit event identifies user, cluster, action, target, evidence
  fingerprint and decision.

### 5.4 Credential separation

- [ ] Use a read-only SSH identity for Watcher/Dashboard collection.
- [ ] Keep mutation credentials only at the approved executor boundary.
- [ ] Test that a read-only identity cannot execute a mutation and that a
  mutation identity is not mounted into read-only services.
- [ ] Redact credentials from logs, AI context, artifacts and release manifests.

## 6. Documentation and distribution (P2)

- [ ] Add and choose an explicit `LICENSE` or `COPYING` file after confirming
  the intended distribution terms.
- [ ] Update `docs/CEPH_AI_HANDOFF.md` to the current HEAD, migration head,
  image/release workflow, runtime owners, autopilot defaults and rollback
  procedure.
- [ ] Ensure README, handoff, production runbook and release manifest describe
  the same runtime model and SHA.
- [ ] Add a documentation freshness check that fails when the handoff records an
  obsolete commit or migration head.

## 7. Ordered execution and exit criteria

Work must proceed in this order:

1. Fix the two known unit-test contract mismatches.
2. Diagnose and fix integration failure.
3. Run the complete Python 3.11/3.12 CI path and unblock quality/release gates.
4. Collect and validate verified River outcomes; keep all candidates shadow-only.
5. Classify the 21 drifted scopes and rerun evaluation.
6. Complete PostgreSQL, RabbitMQ, RBAC and credential-boundary rehearsals.
7. Complete GHCR artifact push/pull and witnessed rollback.
8. Update license and handoff documentation.

Release remains blocked until all of the following are true:

- [ ] Python 3.11 and 3.12 tests pass.
- [ ] Integration passes with JUnit evidence.
- [ ] Quality and release gates execute and pass on a clean runner.
- [ ] No self-learning candidate is promoted without independently verified
  outcome evidence.
- [ ] Drifted scopes have an evidence-backed disposition.
- [ ] PostgreSQL restore/migration/rollback rehearsal passes.
- [ ] Incident Outbox chaos passes with idempotent recovery.
- [ ] RBAC, cross-cluster isolation and credential separation pass.
- [ ] GHCR digest running in staging equals the scanned digest.
- [ ] License, handoff, runbook and manifest match the release SHA.
- [ ] Operator signs off residual risk and rollback readiness.

## 8. Evidence log

| Date | Item | Result | Evidence | Status |
|---|---|---|---|---|
| 2026-09-24 | Independent review of `7abd88ca` | Score 7.0/10; architecture improved but CI is red | Review supplied by operator; CI run `35822665843` | Recorded |
| 2026-09-24 | Known unit failures | `code-repair` resource-policy test and `rbd_copy_volume` contract mismatch | CI annotations supplied by operator | Open |
| 2026-09-24 | Self-learning evidence | River v2: 0 verified outcomes, 0 scored outcomes; candidates remain shadow-only | Runtime report supplied by operator | Open |
| 2026-09-24 | Forecast/anomaly evaluation | 324 comparisons; 21 drifted scopes; HST high false-positive fixture result; RRCF stronger but not production evidence | Review supplied by operator | Open |
| 2026-09-24 | Production readiness | PostgreSQL, RabbitMQ chaos, RBAC, credential separation and witnessed rollback not yet accepted | Review limitations and blocker list | Open |

## 9. Status rules

- `[ ]` No implementation or evidence.
- `[~]` Partial implementation or test evidence; release gate remains open.
- `[x]` Only after command, artifact, environment and rollback note exist.
- `[!]` Blocked by infrastructure, authority, safety window or operator decision.
