# CLAUDE.md

The full system specification is in [docs/spec.md](docs/spec.md). Read it before
making design decisions; [docs/development.md](docs/development.md) describes how
the code maps onto it and what is built so far.

## Workflow

- Never commit to `main` directly. Work on a branch and open a pull request.
- Keep PRs focused on one phase step; CI (ruff, migrations check, pytest) must pass.

## Layout

- `shared/pos_shared/`: ids (UUIDv7), wire schemas, API version. Used by both sides.
- `coordinator/pos_coordinator/`: FastAPI app, models, job queue, auth, budget.
- `worker/pos_worker/`: the worker (heartbeat, jobs, spool, cgroup-aware slots).
- `agent/pos_agent/`: node agent (A/B updater) that runs the worker or coordinator in Docker.
- `deploy/docker/`: images; `install.sh`: installer for all roles; `docs/operations.md`.
- `migrations/`: Alembic. `alembic check` must report no drift.
- `tests/`: pytest; DB tests need PostgreSQL + pgvector (`POS_TEST_DATABASE_URL`).

## Rules that follow from the spec

- The raw layer (`raw_fetch`, `raw_object`, raw store files) is append-only and
  its schema should practically never change. Derived tables are rebuilt, not
  migrated in place.
- Migrations are expand-only within a release (add tables/columns/indexes);
  removals happen in a later release. Statuses are text + CHECK, not PG enums.
- Every derived value carries its stage version and method; never delete, flag.
- Workers never talk to the database; only the coordinator does, and only the
  coordinator talks to the Claude API.
- Node input is untrusted: strict pydantic schemas with `extra="forbid"`.
- Secrets only in environment files, never in the repo.

## Commands

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
POS_DATABASE_URL=... uv run alembic upgrade head
```
