# Ceph AI capability matrix

This matrix describes the supported posture of the application. It is not a
claim that every row has passed live acceptance. A row marked `staging`
requires an environment-specific rehearsal before production use.

| Cluster scope | Ceph major | Deployment mode | Read-only monitoring | Storage inventory | Safe remediation | Convert to cephadm | Production posture |
|---|---|---|---:|---:|---:|---:|---|
| CS-LAB / discovered target | Reef (18) | cephadm | supported | supported | staging | not applicable | staging/live evidence required |
| CS-LAB / discovered target | Reef (18) | systemd/package legacy | supported | supported | staging | staging only | no production conversion claim |
| CS-LAB / discovered target | Squid (19) | cephadm | supported | supported | staging | not applicable | staging/live evidence required |
| CS-LAB / discovered target | Squid (19) | systemd/package legacy | supported | supported | staging | staging only | no production conversion claim |
| Any additional cluster | Unknown/other | any | inspect first | not guaranteed | blocked by policy | blocked | unsupported until capability review |

## Scope rules

- Capability is resolved per cluster, Ceph major version and deployment mode.
- `cephadm` and systemd/package legacy are separate execution contracts; their
  evidence must not be merged.
- Unknown, stale or conflicting discovery is not treated as zero or supported.
- Convert-to-cephadm is one-way and remains a guarded, operator-approved
  staging operation until a real production rehearsal is accepted.
- Safe remediation still requires a typed command, target resolution,
  post-check, audit and the applicable kill switch/rate limit.

## Evidence owner

The operator must attach the discovery output, command exit codes, Ceph major
version, deployment mode, target cluster and witness to the release artifact.
This document should be reviewed whenever a new Ceph major version or
deployment mode is introduced.
