# Development guide

## Setup

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL 16 with
pgvector (or Docker).

```bash
uv sync
docker compose -f deploy/dev/docker-compose.yml up -d   # test database
uv run pytest -q
```

The tests use `POS_TEST_DATABASE_URL`
(default `postgresql+psycopg://pos:pos@localhost:5432/pos_test`) and **wipe that
database**. DB tests are skipped if it is unreachable.

## Docker tests

`deploy/test/e2e_agent.py` builds three versions of the images, pushes them to
a local registry and exercises bootstrap, updates, rollback, blue-green and
agent self-update with real Docker. `deploy/test/install_test.sh` runs
`install.sh` through its lifecycle. Both run in CI (`docker` job) and are
destructive for `pos-*` resources on the Docker host.

## Running locally

```bash
export POS_DATABASE_URL=postgresql+psycopg://pos:pos@localhost:5432/pos
export POS_RAW_STORE_PATH=./data/raw
export POS_ADMIN_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
uv run pos-coordinator migrate
uv run pos-coordinator serve --port 8000

# enrollment code for a node
curl -s -X POST localhost:8000/api/admin/enrollment-codes \
  -H "Authorization: Bearer $POS_ADMIN_TOKEN" -H 'content-type: application/json' -d '{}'

POS_WORKER_COORDINATOR_URL=http://localhost:8000 \
POS_WORKER_ENROLLMENT_CODE=XXXX-XXXX-XXXX-XXXX \
POS_WORKER_STATE_DIR=./data/worker-a \
uv run pos-worker
```

Enqueue dummy work with `POST /api/admin/jobs`
(`{"jobs": [{"type": "dummy_fetch", "payload": {"url": "https://example.invalid/1"}}]}`)
and watch `GET /api/admin/jobs/stats`. Interactive API docs: `/docs`.

## How the code maps onto the spec

| Spec section | Where |
| --- | --- |
| Raw layer, UUIDv7 everywhere | `models.py` (`raw_fetch` partitioned by month, `raw_object`), `pos_shared/ids.py` |
| Job queue via `SKIP LOCKED` | `coordinator/.../queue.py` |
| Node auth (Ed25519 challenge, short-lived tokens, revocation) | `api/nodes.py`, `security.py`, `worker/.../identity.py`, `client.py` |
| Desired state over heartbeat, one-off commands at most once | `api/nodes.py` (`heartbeat`), `worker/.../agent.py` |
| Egress groups (per public IP, IPv6 per /64) | `api/deps.py` |
| Offline buffering (SQLite spool) | `worker/.../spool.py` |
| Dynamic resource use from cgroup limits | `worker/.../cgroup.py` |
| Runtime switches in the DB | `runtime_settings.py`, `setting` table |
| Cost ledger and budget pacing | `budget.py`, `llm_call` table |
| Raw store on the NAS, content-addressed | `rawstore.py`, `api/raw.py` |
| API versioning, incompatible workers refused | `pos_shared/version.py`, `api/deps.py` |
| Signed releases, nodes pin the coordinator key | `pos_shared/signing.py`, `api/releases.py`, `signing_key.py` |
| Node agent: A/B slots, health checks, rollback, self-update | `agent/pos_agent/` |
| Blue-green coordinator, backup before migrations | `agent/pos_agent/services.py`, `coordinator/.../backup.py` |
| Bootstrap script for all roles | `install.sh`, `deploy/docker/` |

### Deviations from the spec

- The agent manages containers itself (Docker Engine API) instead of Docker
  Compose: it needs per-slot containers, health gates and rollback, which
  Compose does not model. `install.sh` still drives everything.
- The worker never gets the Docker socket. The agent updates itself through a
  short-lived helper container from the new agent image, instead of the worker
  recreating the agent; one healthy component always remains.
- Caddy switches traffic through health-checked upstreams (both slots) rather
  than reconfiguration, so a Caddy restart can never point at a stale slot.

### Deviations from the spec's draft names

- `fetch` is a reserved word in PostgreSQL, so the raw fetch table is `raw_fetch`.
- The spec's `worker` table is `node` (a node runs the worker and the agent).
- The admin API uses a bootstrap token (`POS_ADMIN_TOKEN`) until the control panel
  brings user accounts with passkeys/TOTP.

## Status (Phase 1: Foundation)

Done in this step:

- [x] Schema (raw + draft derived tables), Alembic, monthly partitions for `raw_fetch`
- [x] Job queue: lease, extend, results, retry with back-off, lease reaper
- [x] Coordinator API: enrollment, challenge/token auth, heartbeat, jobs, raw upload, admin
- [x] Worker agent: cgroup-aware slots, heartbeat reconcile, commands, spool, dummy handlers
- [x] Cost ledger table and budget pacing logic
- [x] Gate test: dummy jobs end to end; a second worker joins and dies without data loss

- [x] Docker images (amd64 + arm64), release workflow, `install.sh` for all roles
- [x] Node agent with A/B slots, signed releases, blue-green coordinator, agent self-update
- [x] Docker end-to-end test (`deploy/test/e2e_agent.py`) and installer lifecycle test

Next steps:

- [ ] Control panel skeleton (React + Vite + Tailwind + shadcn/ui), user accounts, live updates
- [ ] Domain leases and adaptive per-domain rates (needed before real fetching in Phase 2)
- [ ] Node dashboard (local web UI) and quiet hours; Raspberry Pi SD-card friendly spool
