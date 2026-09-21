# End-to-end release-candidate change classification

Observed on `10.3.55.213:/root/ceph-ai` at `2026-09-19`.

## In-scope feature slice

The modified and untracked files under `shared/natural_language/`, the
natural-language tests and fixtures, `dashboard/chat_client.py`,
`dashboard/routes/chat.py`, `config/settings.py`, `shared/models.py`, the
NL migration, and the `docs/ai/natural-language-*` evidence files form the
current Natural Language Ceph AI slice. They share the `m20260919nlcontext`
migration and must be reviewed and committed together or in dependency order.

## In-scope release evidence

The release manifest, NL rollout scripts, and NL evaluation/benchmark/shadow
artifacts are release evidence for that slice. They contain no secret values
and should be committed with the feature or as a documented evidence commit.

## Corrective change

`dashboard/routes/system_health.py` contains the admin Ceph latency debug
return-path fix. It is covered by `tests/test_ceph_debug.py` (`4 passed`) and
should be isolated as a small corrective commit if the feature slice is not
rebased into the same release.

## Existing/unrelated worktree change

`tests/test_dashboard_nodes.py` was already dirty during this review and is
not included in the NL classification until its owner confirms scope. It must
not be discarded or silently folded into a release commit.

## Commit/push gate

The worktree remains dirty and the runtime is independently managed by
Podman. Commit separation, push, and deployment remain blocked until the
full release suite, migration round-trip evidence, operator ownership, and
rollback approval are recorded.
