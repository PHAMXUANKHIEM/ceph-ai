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
DB pool usage, queue age/backlog, process RSS/cgroup usage, SSH command
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
