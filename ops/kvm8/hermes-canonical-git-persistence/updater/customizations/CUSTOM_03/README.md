# CUSTOM_03 — Hindsight 0.10.x desired state

CUSTOM_03 is a semantic desired-state reconciler. It does not replay, cherry-pick, or depend on any historical MasterTrend commit.

Canonical state:

- HINDSIGHT_SERVER_LINE=0.10.x
- HINDSIGHT_CLIENT_CONSTRAINT=>=0.10.0,<0.11.0
- lock package=hindsight-client
- resolved client must satisfy >=0.10.0 and <0.11.0

The reconciler receives `--repo` and operates only on that worktree. `--check` is read-only. `--apply` changes only recognized Hindsight dependency structures and regenerates `uv.lock` with the project's official `uv lock` command.

Supported results:

- `ALREADY_SATISFIED`
- `REIMPLEMENTED_PASS`
- `BLOCKED_UPSTREAM_INCOMPATIBLE`
- `FAIL_VALIDATION`

It fails closed for missing/ambiguous upstream structures and for a future Hindsight line starting at 0.11.x. Acceptance validates all eight affected files, the lock resolution, Python AST/imports, and the no-regression floor.
