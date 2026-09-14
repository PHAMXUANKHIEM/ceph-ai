# Ceph-AI RCA knowledge: topology-first analysis

This is an operational adaptation of the supplied Dynatrace/RCA report for
Ceph-AI. It is reference knowledge, not log evidence and not an instruction
to execute commands. The complete source report is kept at
`docs/reference/dynatrace-rca-report.html`.

## Core method

1. Treat every signal as an event attached to an entity: log pattern, metric
   anomaly, Ceph health change, RGW request result, Vault response, deployment,
   configuration change, or load-balancer health-check failure.
2. Correlate events by event time and dependency topology. Time correlation
   alone is not proof of a shared root cause.
3. Traverse the dependency graph from the affected request/service toward
   lower-level dependencies. Rank the deepest abnormal entity that explains
   the upstream symptoms; keep other abnormal entities as contributors or
   symptoms.
4. Every conclusion must list evidence IDs/pattern IDs, the time window,
   topology path, and missing evidence. If evidence is absent, contradictory,
   or the host mapping is uncertain, return INSUFFICIENT_EVIDENCE.
5. Recovery means the relevant verification gate passed, not merely that a
   log line stopped appearing. Keep the finding open when live verification is
   unavailable.

## Ceph entity and dependency model

Useful entities: `CEPH_CLUSTER`, `MON`, `MGR`, `OSD`, `PG`, `POOL`,
`RGW_DAEMON`, `RGW_VIP_OR_LB`, `VAULT`, `VAULT_TRANSIT_KEY`, `HOST`,
`CLIENT_IP`, `BUCKET`, and `REQUEST`.

Useful directed relationships:

- `HOST runs_on RGW_DAEMON|OSD|MON|MGR`
- `RGW_VIP_OR_LB routes_to RGW_DAEMON`
- `RGW_DAEMON uses VAULT` and `RGW_DAEMON serves BUCKET`
- `BUCKET backed_by POOL`; `PG belongs_to POOL`; `OSD belongs_to HOST`
- `CLIENT_IP connects_to RGW_VIP_OR_LB`; `REQUEST targets BUCKET`

Normalize aliases before RCA. An advertised RGW address, load-balancer VIP,
container address, hostname, and physical host IP may be different names for
the same service path. For example, do not treat `10.3.53.1` and
`10.3.55.213` as unrelated until the inventory or live topology proves that.

## RGW/Vault decision rules

- `unexpected response from Vault`, token/policy errors, TLS errors, timeout,
  or inability to create/read an SSE-S3 key: candidate root cause is the
  RGW-to-Vault path, authentication, policy, prefix/namespace, TLS, or
  version compatibility. Verify the Vault HTTP status and request ID before
  choosing among them.
- A successful encrypted `ObjectCreated:Put` after a Vault error is recovery
  evidence for that tested path, but also verify `HEAD`/`GET` and live Vault
  health. It does not prove every RGW daemon is healthy.
- `HTTP 405` from an external client is normally a separate client, health
  check, protocol/port, load-balancer, or Internet-scan problem. Do not merge
  it with a Vault or Ceph-cluster problem without a topology and time link.
- `processor->process() returned error r=-22` means an invalid-argument-style
  failure is possible, but the line alone is not a root cause. Require the
  surrounding request, daemon, and preceding error evidence.
- `Cluster is misconfigured! Refusing to trim` is a separate Ceph maintenance
  or configuration candidate until the originating daemon, command, and
  cluster health evidence link it to RGW/S3 symptoms. Do not recommend trim or
  configuration changes from this line alone.

## Problem grouping and recovery

Group events only when they share a plausible topology path and overlapping
event windows. Use a short processing delay to allow late evidence, deduplicate
repeated fingerprints, and preserve separate problems for unrelated client
traffic. A missing correlation group is a data-quality failure, not proof that
the cluster has no related logs.

For RGW/Vault recovery, prefer these gates when available: Ceph health query,
RGW daemon/log verification on the mapped host, Vault health plus token/key
lookup, and a successful encrypted S3 test. For HTTP-method anomalies, require
the 405 rate to return to its baseline and the expected health check/request to
pass for multiple scan intervals.

## LLM boundary

The deterministic evidence, topology, correlation, and recovery gates are the
source of truth. The language model may summarize, rank supported hypotheses,
and propose read-only verification steps. It must not invent hosts, evidence
IDs, topology edges, request outcomes, or a remediation result.
