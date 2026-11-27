# Operations guide

How to install, update and run The Price of Size. Everything is driven by
`install.sh`; nobody has to operate Docker by hand.

## Roles and the reference deployment

| Role | What runs | Typical host |
| --- | --- | --- |
| `database` | PostgreSQL (16/17, distro package) + pgvector, nightly backups, monthly restore test | Database LXC, data on local SSD |
| `coordinator` | Coordinator agent → Caddy + coordinator (blue-green slots) | Coordinator LXC |
| `node` | Node agent → worker (A/B slots) | Worker LXC, remote machines, Raspberry Pis |
| `all-in-one` | All of the above on one machine | Testing, small setups |

LXCs running Docker need the **nesting** feature (Proxmox: Options → Features → nesting).

## Installing

On a fresh Debian/Ubuntu (amd64 or arm64), as root:

```bash
curl -fsSL https://raw.githubusercontent.com/angeeinstein/price-of-size/main/install.sh | sudo bash
```

The menu asks only what the role needs. The same in one line, without prompts:

```bash
# 1. database (prints the URL for the coordinator)
sudo bash install.sh --role database --non-interactive --pg-allow 10.0.0.0/24 --backup-dir /srv/pos/backups
# 2. coordinator
sudo bash install.sh --role coordinator --non-interactive \
  --db-url 'postgresql+psycopg://pos:PASSWORD@10.0.0.11:5432/pos' \
  --raw-store /mnt/nas/pos/raw --backup-dir /mnt/nas/pos/backups --http-port 8080
# 3. a node (enrollment code: POST /api/admin/enrollment-codes, later the control panel)
sudo bash install.sh --role node --non-interactive --coordinator https://pos.example.org --enroll ABCD-EFGH-JKLM-NPQR
```

A node on the coordinator's own machine uses `--coordinator local`: the worker
joins the coordinator's Docker network instead of going out through the tunnel.

`install.sh` installs a **pinned release**: it downloads `release.json` of the
GitHub release (`--version`, default latest), which lists every image by
digest, and refuses anything that is not digest-pinned. Running it again
repairs an installation and leaves healthy containers alone.

Files it writes: `/etc/pos/` (mode 0700) holds `install.conf`, `release.json`
and the env files (`worker.env`, `coordinator.env`, `database.env`, all 0600).
Secrets live only there, never in the repository.

Private images: pass `--registry-user`, `--registry-token` (and
`--registry-server`, default `ghcr.io`) once. They are kept in
`/etc/pos/registry.env` (0600) and handed to the agent, which pulls signed
updates with them; re-runs and `--update` keep them unless new ones are given.

### The control panel and the first admin

The coordinator serves the control panel at its URL. Create the first admin on
the coordinator host:

```bash
docker exec -it pos-coordinator-a pos-coordinator create-user admin --role admin
```

Then sign in, set up two-factor sign-in (Account page) and add further users
(Users page). Admins change things, viewers only look. Sensitive actions
(releases, node targets, revocation, settings, users) ask for the password again
if the last sign-in is older than 10 minutes. Every admin action is in the audit
log. The bootstrap `POS_ADMIN_TOKEN` keeps working for scripts.

### Without the agent ("simple mode")

For machines where you do not want a container with access to the Docker
socket: `--mode simple` runs one worker container, updated by hand with
`install.sh --update`. The coordinator refuses work from versions that are too old.
Any machine with Docker can also run a node without the script:

```bash
docker run -d --name pos-worker --restart unless-stopped --init --shm-size 1g \
  -e POS_WORKER_COORDINATOR_URL=https://pos.example.org -e POS_WORKER_ENROLLMENT_CODE=... \
  -v pos-worker-state:/var/lib/pos-worker ghcr.io/angeeinstein/price-of-size-worker@sha256:...
```

### Remote access (Cloudflare Tunnel)

Caddy listens on `127.0.0.1:<http-port>` of the coordinator host, loopback
only: publishing it on other interfaces would bypass Cloudflare Access, and
direct callers could set `CF-Connecting-IP` themselves. Publish it
with a Cloudflare Tunnel (`cloudflared tunnel ... --url http://localhost:8080`)
and protect the hostname with Cloudflare Access. Nodes send a Cloudflare
Access service token (`--cf-client-id/--cf-client-secret`) in addition to
their own node credentials. No inbound ports are needed anywhere.

## The node agent

Each coordinator and node runs a small **agent** container with access to the
Docker socket. It never talks to the coordinator; it meets its service in a
shared volume (`pos-<role>-exchange`):

| File | Written by | Purpose |
| --- | --- | --- |
| `desired.json` | service | signed manifest of the release to run |
| `status.json` | agent | slots, versions, update progress (reported in heartbeats) |
| `health.json` | worker | proof of a successful heartbeat after a (re)start |
| `signing_key.pub` | service | coordinator signing key, pinned once by the agent |

Every reconcile round (10 s) the agent keeps the active slot running, keeps the
inactive slot stopped, and updates when the signed desired release differs:

* **Worker (A/B):** pull by digest → `pos-worker selftest` in the new image →
  drain the old worker (SIGTERM, up to 120 s) → start the new slot → wait for a
  fresh heartbeat with the new version. On failure the old slot is started again.
* **Coordinator (blue-green):** pull → `pos-coordinator check` → backup
  (`pg_dump` to the backup dir) → migrations → start the new slot beside the
  old one → health check → stop the old slot. Caddy has both slots as upstreams
  with health checks and retries, so the switch does not fail requests.
  Migrations are expand-only, so the old version keeps working on the new schema.
* **Agent self-update:** only once the service runs the desired version. A
  short-lived helper container from the new agent image swaps the agent and
  restores the old one if the new one does not report healthy.
* A failed version is not retried for 6 hours. The previous version stays in
  the other slot until the next update.

### Trust model

* The coordinator has an Ed25519 **signing key** (`pos-coordinator-data`
  volume). It signs every release an admin registers. **Back it up**: losing it
  means re-enrolling every node.
* Nodes receive the public key at enrollment; the agent pins it on first sight
  in its private volume, which the worker cannot write. Afterwards it runs only
  releases signed by that key, so a compromised registry or a compromised
  worker cannot make it run other images. (The first pin is trust-on-first-use.)
* Images are always referenced by digest; Docker verifies the content.

## Releases and updates

1. Bump `shared/pos_shared/version.py`, merge, and push the tag `vX.Y.Z`. The
   release workflow builds multi-arch images, pins them by digest in
   `release.json` and publishes the GitHub release.
2. Register the release with the coordinator (control panel later; for now):
   ```bash
   curl -X POST https://pos.example.org/api/admin/releases -H "Authorization: Bearer $POS_ADMIN_TOKEN" \
     -H 'content-type: application/json' -d @release.json
   ```
3. Roll out: set one canary node's target (`PATCH /api/admin/nodes/<id>` with
   `{"target_version": "X.Y.Z"}`), watch its `agent_status`, then the rest.
   The coordinator itself: `PUT /api/admin/settings/coordinator.target_version`.
4. **Rollback** is the same with the previous version (already present in the
   other slot, so it is just a container start).

`install.sh --update` updates the installer-managed parts (agent bootstrap,
simple-mode worker). In agent mode the coordinator steers versions.

## Crawling

Queue URLs (the control panel will do this; for now the admin API):

```bash
curl -X POST https://pos.example.org/api/admin/crawl/urls -H "Authorization: Bearer $POS_ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"source": "ikea", "urls": ["https://www.ikea.com/at/de/p/..."]}'
```

URLs are normalised (tracking parameters dropped) and queued once. Per domain:

* robots.txt is fetched first and stored; with `respect_robots_txt` on (default)
  disallowed URLs fail without a request. Crawl-delay / Request-rate cap the rate.
* Rate slots are scheduled per (domain, egress group), starting at
  `base_rate_rps` (1 request per 5 s). After 20 clean responses the rate rises
  by 10 % up to `max_rate_rps`; 429/503/timeouts/rising latency halve it and
  pause the domain (Retry-After honoured, cooldown doubling up to 6 h).
* New domains get one request at a time until 10 pages answered (slow start).
* CAPTCHAs, bot walls and five 403s in a row block the domain. Nothing tries to
  get around a block. `GET /api/admin/domains?blocked=true`, history in
  `GET /api/admin/domains/<domain>/events`, unblock or cap a domain with
  `PATCH /api/admin/domains/<domain>` (`{"unblock": true}`, `{"rate_rps": 0.1}`).
* The crawler identifies itself with `user_agent` (default `PriceOfSizeBot/0.1`).

### Sources and discovery

A source describes where URLs come from. Sitemaps advertised in robots.txt,
explicit sitemaps and seed pages are fetched through the same polite schedule;
sitemap indexes are followed, and URLs matching `product_url_pattern` are queued
as pages (other hosts than the sitemap's only with `allow_domains`).

```bash
curl -X PUT https://pos.example.org/api/admin/sources/ikea -H "Authorization: Bearer $POS_ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"type": "scraper", "config": {
    "domains": ["www.ikea.com"], "product_url_pattern": "/at/de/p/[^/]+-\\d+/?$", "priority": 0}}'
curl -X POST https://pos.example.org/api/admin/sources/ikea/discover -H "Authorization: Bearer $POS_ADMIN_TOKEN"
```

The first `discover` of a new domain queues its robots.txt (and seeds); once
robots.txt is known, `discover` again queues the advertised sitemaps.

## Backups

* **Database role:** `pos-db-backup.timer` dumps the database nightly
  (custom format, newest 14 kept) to `<backup-dir>/database`;
  `pos-db-restore-test.timer` restores the newest dump into a scratch database
  every month. Check with `systemctl list-timers 'pos-*'` and
  `journalctl -u pos-db-backup`.
* **Before every coordinator migration** the agent runs `pos-coordinator backup`
  into `<backup-dir>/coordinator` (newest 20 kept).
* **Also back up:** the `pos-coordinator-data` volume (signing key), `/etc/pos`,
  the raw store and HTML archive on the NAS, and Proxmox snapshots of the LXCs.

Restore a dump: `pg_restore --clean --if-exists --no-owner -d pos <file>.dump`.

## Uninstalling

```bash
sudo bash install.sh --uninstall                 # everything on this machine, data kept
sudo bash install.sh --uninstall --role node     # only the node on a multi-role machine
sudo bash install.sh --uninstall --purge         # also delete identities, signing key, /etc/pos
```

The PostgreSQL database, the raw store and backups are never deleted by the script.

## Troubleshooting

```bash
docker ps --filter label=org.price-of-size.managed=true   # what the agent runs
docker logs pos-agent-worker                               # agent decisions
docker exec pos-agent-worker pos-agent status              # agent state (slots, failures)
docker logs pos-worker-a                                   # the worker itself
```

* *Worker never becomes healthy after an update:* the agent rolls back on its
  own; the error is in the node's `agent_status.update_error`.
* *"not signed by the pinned key":* the coordinator's signing key changed
  (restored from scratch?). Re-enroll the node: `install.sh --uninstall --purge
  --role node`, then install with a new code.
* *Docker not running in an LXC:* enable nesting.
* *`/healthz` says `"raw_store": false`, log says "raw store ... is not this
  installation's store":* the raw store directory does not hold the marker
  (`.pos-rawstore`) the database recorded on first start, usually because the
  NAS is not mounted. Nothing is lost meanwhile: raw uploads are refused (nodes
  retry), fetch jobs stay queued and derivation waits. Mount the NAS; it
  recovers by itself. After *moving* the raw store on purpose, accept the new
  location once with `docker exec pos-coordinator-a pos-coordinator
  raw-store-adopt`.
