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

## Control panel

React + TypeScript + Vite + Tailwind in `panel/`. During development:

```bash
cd panel && npm ci && npm run dev     # proxies /api to the coordinator on :8000
```

For local HTTP set `POS_COOKIE_SECURE=false` on the coordinator (session cookies
are `Secure` by default). `npm run build` writes `panel/dist`, which the
coordinator serves; `npm run e2e` drives the panel in Chromium (see
`panel/e2e/smoke.mjs`; CI runs it against a real coordinator).

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
export POS_SIGNING_KEY_PATH=./data/signing_key.pem   # default is /var/lib/pos/... (images)
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
| Raw store on the NAS, content-addressed; identity marker (unmounted NAS is detected) | `rawstore.py`, `api/raw.py` |
| API versioning, incompatible workers refused; replies read leniently, requests strictly | `pos_shared/version.py`, `pos_shared/api.py` (`Strict`, `Reply`), `api/deps.py` |
| Signed releases, nodes pin the coordinator key | `pos_shared/signing.py`, `api/releases.py`, `signing_key.py` |
| Node agent: A/B slots, health checks, rollback, self-update | `agent/pos_agent/` |
| Blue-green coordinator, backup before migrations | `agent/pos_agent/services.py`, `coordinator/.../backup.py` |
| Bootstrap script for all roles | `install.sh`, `deploy/docker/` |
| Domain rate limits per egress group, adaptive back-off, cooldowns | `coordinator/.../crawl/scheduler.py`, `crawl/ratecontrol.py`, `crawl/feedback.py` |
| robots.txt fetched first, enforced per setting, Crawl-delay | `crawl/robots.py`, `crawl/scheduler.py` |
| Blocks (CAPTCHA, bot walls, repeated 403) logged, no evasion | `worker/.../fetch/blocks.py`, `crawl/feedback.py`, `domain_event` |
| Raw evidence capture (JSON-LD, tables, breadcrumbs, price blocks, ...) | `worker/.../fetch/snippets.py` |
| Bounded fetches: size after decompression, total deadline, public addresses only, same-host redirects | `worker/.../fetch/http.py` |
| Per-domain spacing on the worker (overdue slots never burst) | `worker/.../fetch/handlers.py` (`_DomainGate`) |
| HTML retention policy | `crawl/scheduler.py` (`_keep_html`), `fetch/handlers.py` |
| Units, number formats per locale, net/gross, product vs. package | `pos_shared/units.py` |
| Extraction tiers 1 (JSON-LD) and 3 (spec tables, dl, text) | `pos_shared/extract/` (`jsonld.py`, `spectext.py`, `pipeline.py`) |
| Price traps (unit/monthly/net/used/placeholder prices, packs) | `pos_shared/extract/price_rules.py` |
| Derive stage: products, offers, measurements; versions on every row | `coordinator/.../derive/stage.py` |
| Product identity (GTIN check digit, brand+MPN, URL) | `derive/stage.py` (`match_key`) |
| Currency conversion at fetch-day ECB rates, never guessed | `derive/fx.py`, `fx_rate` table |
| Plausibility (size, density, gross < net), flag never delete | `derive/stage.py` (`measurement_flags`, `cross_checks`) |
| Rebuild drill: derived layer reproducible from raw | `derive/stage.py` (`rebuild`), `pos-coordinator rebuild-derived` |
| Log-log explorer | `panel/api.py` (`/api/panel/scatter`), `panel/src/pages/Explorer.tsx` |

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

- [x] Control panel: accounts (Argon2id, TOTP), sessions + CSRF, roles, re-auth, audit;
      overview (live via SSE), nodes, jobs, domains, sources, releases, settings, users
- [x] Panel: data explorer (log-log price vs. mass/volume/length with fit)
- [ ] Panel: QA/gold labelling, costs, AI parser chat, passkeys
- [x] Polite fetching: per-(domain, egress group) rate slots, AIMD back-off, robots.txt,
      blocks, raw evidence capture, HTML retention (Phase 2 groundwork)
- [x] Discovery: sitemaps (from robots.txt or explicit), indexes, gzip, per-source URL patterns
- [ ] Discovery from category pages (links), scheduled re-discovery and re-fetch
- [ ] Browser fetching (Playwright) for pages without data over plain HTTP
- [ ] Node dashboard (local web UI) and quiet hours; Raspberry Pi SD-card friendly spool

### Phase 2: extraction and the derived layer

The derive stage runs inside the coordinator. It walks `raw_fetch` in
`(received_at, id)` order behind a cursor (`derive_cursor`), leaving a settle
window so that rows committed late are not skipped. For each fetch it reads
the raw evidence from the NAS, runs the extraction tiers and upserts the
product (by match key), its offers (one per fetch) and its measurements. Every
row carries the parser, unit and validator versions. Bumping
`derive.stage.VERSION` means the derived layer is rebuilt, not migrated:

```bash
pos-coordinator rebuild-derived   # truncate derived tables, re-run from raw, compare counts
pos-coordinator fx-refresh        # ECB reference rates (also fetched daily)
```

- [x] Units and numbers (German/English formats, ranges, fractions, inches)
- [x] JSON-LD Product/Offer/AggregateOffer/ProductGroup, breadcrumbs, additionalProperty
- [x] Spec tables and definition lists: dimensions, weight, product vs. package
- [x] Price traps flagged; offers in foreign currency converted at the fetch-day rate
- [x] Canonical price: median over pages (latest observation each) of valid offers in EUR
- [x] Cross-source check (factor 3 from the median; two disagreeing pages: neither) and
      price jumps between visits (factor 10), recomputed as offers arrive
- [x] Rebuild drill reproduces the derived layer from raw
- [ ] Tier 2 (microdata/RDFa), tier 4 (LLM extraction within budget)
- [ ] Classification and embeddings; per-category size scalar
