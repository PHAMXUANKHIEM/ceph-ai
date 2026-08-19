"""Pluggable log-source adapters for Log Intelligence & AI RCA
(Plan/log-intelligence-rca-plan.md, tầng T1).

The whole point of this package is constraint R2 in that plan: ceph-aiops
is a log ANALYZER, not a log STORE. Whatever the RCA team eventually
standardizes on for centralized logging, only a file in here changes --
the fingerprint/triage/AI layers downstream (`watcher/log_intel.py`) never
learn where a line came from.

Two adapters ship today:

- `ssh_tail`: reuses the exact SSH access `watcher/ceph_log.py` already
  has. Needs no new infrastructure at all, so the evidence base starts
  building on day one rather than waiting on a log-store rollout.
- `loki`: the chosen centralized store (plan section 11.1). Selected by
  flipping `settings.log_intel_source` to "loki"; nothing else changes.
"""

from watcher.log_source.base import LogRecord, LogSourceError, LogSourceResult

__all__ = ["LogRecord", "LogSourceError", "LogSourceResult", "get_log_source"]


def get_log_source(name: str):
    """Resolve `settings.log_intel_source` to an adapter module.

    Imports lazily so the `loki` adapter's own httpx dependency is only
    touched when an operator actually selects it -- an ssh-mode deployment
    must not fail to start over a log store it doesn't use.
    """
    if name == "ssh":
        from watcher.log_source import ssh_tail

        return ssh_tail
    if name == "loki":
        from watcher.log_source import loki

        return loki
    raise LogSourceError(
        f"Nguồn log không hợp lệ: {name!r} (chỉ hỗ trợ 'ssh' hoặc 'loki')"
    )
