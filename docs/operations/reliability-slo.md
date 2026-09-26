# Ceph-AI Reliability and Operations Contract

## Runtime owner

On the current server, the runtime owner is the Podman Compose stack controlled
by `ceph-ai-containers.service`. The core services are `dashboard-web`,
`worker`, `watcher`, `remediation-watcher`, `telegram-ai` and `full-executor`.
Operators must not run the same services concurrently through both Podman and
the legacy `ceph-ai-*.service` units. Restart the stack with:

```text
systemctl restart ceph-ai-containers.service
```

The Dashboard endpoint `/api/system/reliability` exposes the owner contract,
heartbeat state, restart/reconnect/reconcile checks and the recommended
restart command.

## Availability model: single node, no HA

This release is a **single-node deployment without high availability**. It
is part of the release contract, not a temporary gap to be read around:

| Component | Where it runs | Redundancy | Loss of it means |
|---|---|---|---|
| Dashboard, Watcher, Worker, Remediation Watcher, Telegram AI, Full Executor | One Podman host (`ceph-ai-containers.service`) | None | No monitoring, alerting, diagnosis or remediation until the host is back |
| RabbitMQ (`rabbitmq` container) | Same host | None | Incident delivery pauses; the Incident Outbox holds committed incidents and republishes after recovery |
| PostgreSQL | Separate database host | Not verified by this project | Every service fails closed; no incident or action state can be written |
| Pre-migration backups (`/var/backups/ceph-ai`) | Same Podman host | None (plan 4.3 moves them off-host) | Backups are lost together with the host |

Consequences operators must plan for:

- **The Ceph clusters keep running** when Ceph AI is down; only its
  observation and remediation stop. Ceph's own health alerts remain the
  fallback signal.
- **No automatic failover.** Recovery is a restart on the same host or a
  rebuild from the release artifact (image digest) plus a database restore.
- **RTO/RPO are not yet measured** (plan 4.1 and 4.3). Until they are, the
  SLOs below describe the running system and are not a recovery promise.
- **Maintenance of the host is an outage** of Ceph AI; schedule it and tell
  the operators who rely on its alerts.

Moving to HA (a second application host, a replicated broker and a managed
or replicated PostgreSQL) is a separate design decision, not part of this
release.

## SLO and error budget

The initial 30-day contract is:

| Signal | SLO | Error budget |
|---|---:|---:|
| Dashboard snapshot freshness | 99.0% | 1.0% |
| Incident processing | 99.5% | 0.5% |
| Notification delivery | 99.0% | 1.0% |
| Post-check completion | 99.5% | 0.5% |

The contract is advisory until an operator accepts the production baseline.
The system does not auto-promote, restart or rollback when the budget is
exhausted.

## Diagnostics and alerts

`/api/system/reliability` is read-only and reports API p95, collector lag,
DB pool usage and current database size, queue age/backlog, process RSS/cgroup usage, SSH command
counts/p95, failed deploys in the last 24 hours and federated mapping
reconcile state. It emits bounded alert codes for stale snapshots, queue
backlog, DB pool exhaustion, collector/reconnect failure, stale service
heartbeats and failed deploys.

## Restart/reconnect/reconcile

Heartbeat checks cover process restart/liveness. Collector and SSH counters
cover reconnect/failure signals. Durable outbox/action queues and federated
role-mapping state cover reconcile/backlog state. Recovery remains operator
approval-gated; no diagnostic request executes a remote Ceph command.

## 24-hour soak

Run the read-only soak collector after the operator approves the window:

```text
.venv/bin/python scripts/reliability_soak.py \
  --duration-hours 24 --interval-seconds 60 \
  --output docs/benchmark/reliability-soak-<date>.json
```

The report ends in `REVIEW_REQUIRED`; it must be reviewed and signed off by an
operator before the production readiness checkbox can become complete.
