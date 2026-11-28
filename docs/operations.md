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

Inside an LXC, Docker containers see the host's CPUs and RAM, not the LXC's (its
limits sit on the LXC's own cgroup). The installer therefore writes the machine's
CPUs and RAM, as it sees them, into the worker's environment
(`POS_WORKER_CPU_LIMIT`, `POS_WORKER_MEMORY_LIMIT_MB`), and the worker sizes its
concurrency by the smaller of those and its container's own limits. After
resizing a node's LXC, run the installer on it again (it keeps its settings) so
the worker picks up the new size.

## Installing

On a fresh Debian/Ubuntu (amd64 or arm64), as root:

```bash
curl -fsSL https://raw.githubusercontent.com/angeeinstein/price-of-size/main/install.sh | sudo bash
```

The menu asks only what the role needs. The same in one line, without prompts:

```bash
# 1. database (prints the URL for the coordinator)
sudo bash install.sh --role database --non-interactive --pg-allow 10.0.0.0/24 --backup-dir /srv/pos/backups
# 2. coordinator (--listen 0.0.0.0: reachable from the LAN, e.g. by cloudflared in
#    another LXC at 10.0.0.5; omit both for this machine only)
sudo bash install.sh --role coordinator --non-interactive \
  --db-url 'postgresql+psycopg://pos:PASSWORD@10.0.0.11:5432/pos' \
  --raw-store /mnt/nas/pos/raw --backup-dir /mnt/nas/pos/backups --http-port 8080 \
  --listen 0.0.0.0 --trusted-proxy 10.0.0.5
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

**Settings** is a form, grouped by topic, with the right input for each value
(switches, numbers with their unit and range, choices, models with their prices).
A changed value shows its default and a "reset" link. "Edit as JSON" lists every
key as raw JSON, including the few nothing reads yet. The server checks every
value whichever way it is entered. Secrets (the Claude API key) are not settings:
they live only in the environment files.

**HTML archive:** the full HTML of every page is kept until the archive reaches
`html_archive_quota_gb` (100 GB by default); after that, only the first pages of
each domain and a random sample. On a small coordinator disk, lower the quota
well below the free space (Settings, Storage and retention).

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

### Remote access (Cloudflare Tunnel) and the LAN

Caddy listens on `<listen>:<http-port>` of the coordinator host (`--listen`,
`--http-port`, default `127.0.0.1:8080`). Publish it through a Cloudflare Tunnel
and protect the hostname with Cloudflare Access; nodes elsewhere send a Cloudflare
Access service token (`--cf-client-id/--cf-client-secret`) in addition to their own
node credentials. No inbound ports are needed anywhere.

* **cloudflared on the coordinator host:** keep `--listen 127.0.0.1` and point the
  tunnel at `http://localhost:8080`.
* **cloudflared in another LXC (one tunnel for all your services):** install with
  `--listen 0.0.0.0` (or the coordinator's LAN address) and
  `--trusted-proxy <cloudflared IP>`, and point the tunnel's public hostname at
  `http://<coordinator IP>:8080`.

With `--listen` beyond loopback the control panel and the node API are also
reachable from the LAN at `http://<coordinator IP>:8080`, without Cloudflare
Access: the panel's sign-in (password, two-factor) and the nodes' own credentials
still apply, and a node on the LAN can use that URL instead of the tunnel. Over
plain HTTP from a LAN address the session cookie is set without `Secure` (the
browser would not send it back otherwise); everywhere else it keeps it.

The visitor's address (sign-in rate limits, the audit log, a node's egress group)
comes from Caddy: it believes `CF-Connecting-IP` only from `--trusted-proxy` (on
loopback only: from every caller, since only this machine can call) and sets its
result on every upstream request as `X-Pos-Client-Ip`, overwriting whatever a
caller sent; the coordinator reads nothing else (not `CF-Connecting-IP` or
`X-Forwarded-For` themselves, which a caller could send). Without
`--trusted-proxy`, visitors through a cloudflared on another host appear with that
host's address. `GET /api/admin/connection` shows how the coordinator sees a
request (address, egress group, HTTPS) to check the setup.

Callers on a local network (RFC 1918, loopback, link-local, IPv6 ULA) share one
egress group, `lan`: they leave through the same public address, so they share one
budget per domain. A node of the same network that comes in through the tunnel is
grouped by its public address instead; keep the nodes of one site on one path.
A node's group is recomputed at every heartbeat, so a change of these rules
reaches nodes enrolled before it.

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

**Publishing a release.** Bump `shared/pos_shared/version.py`, merge, and publish
a release with the tag `vX.Y.Z` on `main` (GitHub's release page, or push the
tag). The release workflow builds multi-arch images, pins them by digest in
`release.json` and attaches it and `install.sh` to the release (10–15 minutes).

**Updating: the panel.** Releases page, card "Updates": the coordinator asks
GitHub for the latest release every few hours (`POS_UPDATE_CHECK_INTERVAL_H`,
default 6; 0: only on request), or right away with **Check now**. **Update to
X.Y.Z** then does everything, with each step shown as it happens:

1. downloads that release's `release.json`, checks it (version, every image pinned
   by digest, no image moved to another repository) and registers and signs it;
2. the coordinator: its agent downloads the images, checks the new version, backs
   up the database **if the release has migrations** (keeping the newest 3 such
   dumps in the backup directory), migrates, starts the new version next to the
   old one and switches once it answers (blue-green);
3. one node (the first online one): its agent drains the old worker, starts the
   new one and switches once it is healthy;
4. the other nodes. Offline nodes keep the target and update when they are back.

A step that fails stops the rollout ("failed", with the reason); the agent of that
machine has rolled back to the version before by then, and tries the failed version
again only after 6 hours. **Stop the rollout** leaves nodes that have not started
on their version. The updates come from `POS_UPDATE_REPOSITORY` (default
`angeeinstein/price-of-size`, in the coordinator's environment file, not in the
panel: an admin account cannot point the installation at other code).

**Updating: SSH.** `install.sh --update` on the coordinator repairs the
coordinator's installation, then does the same as the Update button through the
coordinator's API, printing each step. If an update is already running, it follows
it. On a node it only repairs the node: nodes are updated by their coordinator.

**By hand.** Releases page: paste a `release.json`, register it, **Run on
coordinator**, then set a node's target on the Nodes page, then the others. The
API equivalents: `POST /api/admin/releases`, `PUT
/api/admin/settings/coordinator.target_version`, `PATCH /api/admin/nodes/<id>`
with `{"target_version": "X.Y.Z"}`; the update API is `GET /api/admin/updates`,
`POST /api/admin/updates/check`, `/start` (`{"version": "X.Y.Z"}`) and `/cancel`.

**Rollback** is the same by hand with the previous version (still present in the
other slot, so it is just a container start). A rolled-back coordinator keeps the
newer database schema (`migrate` says so and changes nothing): migrations only
add, and removals land a release after the code stopped using them, so the
previous release runs on it. Go back one release at a time; for anything further,
restore the pre-migration dump in the backup directory.

## Crawling

Queue URLs (the control panel will do this; for now the admin API):

```bash
curl -X POST https://pos.example.org/api/admin/crawl/urls -H "Authorization: Bearer $POS_ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"source": "ikea", "urls": ["https://www.ikea.com/at/de/p/..."]}'
```

URLs are normalised (tracking parameters dropped) and queued once. Per domain:

* robots.txt is fetched first and stored; with `respect_robots_txt` on (default)
  disallowed URLs fail without a request, and an unreachable robots.txt holds a
  domain's pages back until it can be read. With it off, pages go regardless
  (robots.txt is still fetched and kept). Crawl-delay / Request-rate cap the rate.
* Rate slots are scheduled per (domain, egress group), starting at
  `base_rate_rps` (1 request per 5 s). After 20 clean responses the rate rises
  by 10 % up to `max_rate_rps`; 429/503/timeouts/rising latency halve it and
  pause the domain (Retry-After honoured, cooldown doubling up to 6 h).
* New domains get one request at a time until 10 pages answered (slow start).
* A challenge page (CAPTCHA, bot wall) pauses a domain (`block_suspected`); more
  of them in a row, or a run of 403s, block it. CAPTCHA widgets on ordinary pages
  (a newsletter form) do not count. The HTTP fetch runs no JavaScript, so it does
  not get past a challenge page (such as a Cloudflare "Enable JavaScript and
  cookies to continue" check) by waiting or retrying.
* The block policy is a set of runtime settings (panel: Settings), each also
  settable per domain (panel: Domains, or `PATCH /api/admin/domains/<domain>`
  with `{"block": {"block_challenge_run": 4}}`; `null` clears an override):

  | Setting | Default | Meaning |
  | --- | --- | --- |
  | `block_challenge_run` | 2 | challenge pages in a row before a block |
  | `block_suspect_pause_s` | 3600 | pause after the first one (workers hold back as long) |
  | `block_forbidden_run` | 5 | 403s in a row that count as a block |
  | `block_retry_after_h` | null | retry a blocked domain after this many hours; null: only when unblocked by hand (per domain, 0 means never) |

  A retried domain keeps its refusal count: if the next answer is a challenge
  again it is blocked again at once (`unblocked` / `auto_retry` and `blocked`
  events), a clean answer resets it.
* robots.txt is the site's wish, not a per-IP budget: its `Crawl-delay` (and a
  per-domain `rate_rps` override) hold across all nodes and egress groups.
* Workers fetch public addresses only: a sitemap or redirect pointing into the
  node's own network (router, NAS, cloud metadata) is refused
  (`blocked_address`). Workers follow no page redirects themselves: a redirect
  within the same site (`www.` or not, up to 5 hops) is queued as its own job,
  through robots.txt and the rate schedule, so it cannot lead to a disallowed
  path. robots.txt itself follows its redirects, as RFC 9309 asks. For a lab shop on a private address, set
  `POS_WORKER_ALLOW_PRIVATE_TARGETS=1` on that node. `GET /api/admin/domains?blocked=true`, history in
  `GET /api/admin/domains/<domain>/events`, unblock or cap a domain with
  `PATCH /api/admin/domains/<domain>` (`{"unblock": true}`, `{"rate_rps": 0.1}`,
  `{"block": {...}}`).
* The crawler identifies itself with `user_agent` (default `PriceOfSizeBot/0.1`).

### Sources and discovery

A source describes where URLs come from. Sitemaps advertised in robots.txt,
explicit sitemaps and seed pages are fetched through the same polite schedule;
sitemap indexes are followed, and URLs matching `product_url_pattern` are queued
as pages (other hosts than the sitemap's only with `allow_domains`).
`sitemap_url_pattern` limits which child sitemaps of an index are followed: IKEA's
index lists about 1,500 sitemaps for every country and catalogue, each up to 50 MB;
`prod-de-AT_\d+\.xml$` keeps the six with Austrian products (about 30,000 pages).

Pages are read in tiers: JSON-LD, then data shops ship to the browser as JSON
(Next.js and similar "app state", e.g. IKEA's package dimensions and weight), then
spec tables and text.

```bash
curl -X PUT https://pos.example.org/api/admin/sources/ikea -H "Authorization: Bearer $POS_ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"type": "scraper", "config": {
    "domains": ["www.ikea.com"], "sitemap_url_pattern": "prod-de-AT_\\d+\\.xml$",
    "product_url_pattern": "/at/de/p/[^/]+-\\d+/?$", "priority": 0}}'
curl -X POST https://pos.example.org/api/admin/sources/ikea/discover -H "Authorization: Bearer $POS_ADMIN_TOKEN"
```

For a domain whose robots.txt is not known yet, `discover` queues robots.txt
(and the seeds) and the sitemaps follow by themselves once it arrives: those its
`Sitemap:` lines name on the shop's own hosts, or `/sitemap.xml` if there are
none. Sitemaps and pages fetched recently are not queued again, so `discover`
can be run as often as wanted.

### API sources: Wikidata

The upper end of the size range (aircraft, ships, spacecraft, bridges, buildings)
comes from Wikidata's query service: an `api` source with one SPARQL query per
class, paged, each response stored as raw evidence and read into one product per
item (cost or price with its currency and the date it refers to; mass, length,
width, height, diameter, normalised to kg and m by Wikidata).

```bash
curl -X PUT https://pos.example.org/api/admin/sources/wikidata -H "Authorization: Bearer $POS_ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"type": "api", "config": {
    "adapter": "wikidata", "page_size": 1000,
    "classes": [
      {"id": "Q15056995", "label": "aircraft model"},
      {"id": "Q11446", "label": "ship"},
      {"id": "Q40218", "label": "spacecraft"},
      {"id": "Q26540", "label": "artificial satellite"},
      {"id": "Q12280", "label": "bridge"},
      {"id": "Q29048322", "label": "vehicle model"},
      {"id": "Q41176", "label": "building", "subclasses": false}]}}'
curl -X POST https://pos.example.org/api/admin/sources/wikidata/discover -H "Authorization: Bearer $POS_ADMIN_TOKEN"
```

* A class includes its subclasses unless `"subclasses": false`; very broad classes
  (buildings) exceed the query service's 60 s limit with them and fail as errors.
  Only items that have a cost (P2130) or price (P2284) are queried: a few hundred
  per class.
* API jobs are rate-limited and back off like pages, but robots.txt does not apply
  to them: query.wikidata.org disallows `/sparql` for crawlers that would follow
  query links, while the query service itself is meant for programs. Its
  User-Agent policy asks for a contact: set `user_agent` to something like
  `PriceOfSizeBot/0.1 (https://example.org/bot; bot@example.org)`.
* Costs are nominal. The newest cost of an item is its price; older ones are kept
  (`older_cost`). A cost converts to EUR at the ECB rate of the date it refers to
  (its `point in time`, else the item's inception, first flight, launch or service
  entry, noted in the offer's `fields.price.as_of_method`). Costs before 1999 or in
  historical currencies (gold mark) have no reference rate and stay without a EUR
  value (`no_fx_rate`, `no_currency`) until an inflation stage exists.
* `endpoint` points the source at a mirror or a self-hosted query service.

### Categories: taxonomy and shop mappings

Products are classified into the Google Product Taxonomy (about 5,600 categories,
English and German names) plus a small extension for what shops do not sell
(buildings and structures, land, bulk materials, fluids, spacecraft, rail
vehicles; ids `pos-...`). Import it once, and again when Google publishes a new
version (idempotent; categories Google dropped are kept):

```bash
pos-coordinator taxonomy-import                 # downloads the en-US and de-DE files
pos-coordinator taxonomy-import --en taxonomy-with-ids.en-US.txt --de taxonomy-with-ids.de-DE.txt
```

The first tier of classification is the shops' own categories: each shop's
breadcrumb path is mapped once (panel: Categories, most frequent paths first;
or `PUT /api/admin/category-mappings` with `source`, `path` and `node_id` or
`external_id`; without either the path is marked as having no fitting category).
The maintenance loop then classifies every product under that path, re-classifies
them when a mapping changes, and never overrides a classification made by hand.
Mappings are curated data: a rebuild of the derived layer keeps them, and the
previous mapping of a path stays as history.

### Classification tiers

A product has one current classification; the tiers, most trusted first, are
manual, LLM pick, shop mapping and k-NN. A tier never replaces a more trusted one.
Every row names its tier, classifier version and confidence. Results that do not
become current are kept as attempts, so the same version never asks twice.

* **LLM mapping proposals.** Shop category paths with at least
  `category_mapping_min_products` products (default 3) and no mapping go to the
  `category_mapping` model (Sonnet). Each request carries a few product titles and
  the 20 taxonomy categories nearest to the products' embeddings. The answer
  becomes a mapping with method `llm`, a confidence and a one-line reason.
  Panel: Categories, "LLM proposals": *Confirm* turns it into a manual mapping;
  *Map* changes it. The LLM never touches a manual mapping. Switch:
  `category_mapping_proposals`.
* **k-NN.** It learns from the products with a manual, LLM or shop-mapping label:
  every manual and LLM label, plus at most 20 shop-mapping labels per shop and
  category. It needs at least 50 examples before it starts. The 15 nearest
  examples vote; a vote share of at least `classify_knn_min_confidence` (0.8)
  classifies the product. "Check k-NN" in the panel shows accuracy and coverage
  per threshold, measured against the LLM and manual labels.
* **LLM picks.** Products without a classification, or with a k-NN or shop-mapping
  one below `classify_llm_below_confidence` (0.8), go to the default model (Haiku,
  batch). The model picks from the 20 nearest categories or answers "none".
  Batches hold up to `classify_llm_per_round` products (1000). Switch:
  `classify_llm`. While the k-NN classifier is active, a product waits for its
  k-NN attempt first. Products in a shop category whose mapping is still being
  proposed also wait.

The prompts hold only what stays the same for a product (no price), so a rebuild
asks the same questions and gets the answers from the ledger at no cost. A new
taxonomy import, another model, or *Run again* in the panel (a new generation)
runs the classifiers again. LLM questions that are unchanged are answered from the
ledger; only new ones cost budget.

Exchange rates: the ECB reference rates are fetched daily. An offer in another
currency that arrives before its rate is stored with `no_fx_rate` and converted
(and its product's price recomputed) once the rate is there;
`pos-coordinator fx-refresh` does both at once.

### LLM extraction (tier 4)

A page with a title but no price, or no sizes, after the other tiers is read by
the LLM: the default model in batches, at most `llm_extraction_per_round` (500)
pages per batch, switch `llm_extraction`. The model gets a condensed text of the
captured snippets (title, description, breadcrumbs, price texts, spec tables). It
copies the price as written and the size and weight lines with their labels; the
same parsers as for every other tier turn them into numbers. Its answer only fills
what was missing, with method `llm`. Answers are stored per raw fetch
(`llm_extraction`, kept across rebuilds), and the page is derived again when its
answer arrives. If the raw store is away at that point, the answer waits and is
applied as soon as the store is back. If the page's evidence is missing from the
store (a partial restore), the page keeps what it has and the answer is tried
again every 10 minutes (`applied_at` stays empty, `tried_at` shows the last
try). A page whose evidence is missing when its request is due waits the same way,
behind the others.

### Per-site parsers (tier 2)

Once the LLM tier has read `parser_min_pages` (12) product pages of a shop with the
full HTML archived (distinct URLs: a page fetched again counts once), the
`parser_generation` model (Sonnet) writes a parser for the shop. The parser is declarative: CSS selectors, optionally with an attribute and a
short regular expression, for title, price, breadcrumbs and size lines. The model
sees 5 of the pages as compact HTML, with what the LLM tier found on each. The
parser is then run on up to 15 further pages. A page passes when the parser finds
every price and size the LLM found there, and no price or size the LLM did not
find. With a score of at least
`parser_min_score` (0.8) it becomes the shop's active parser, unless
`parser_auto_activate` is off. From then on the derive stage reads that shop's
pages with it, without the LLM (except a page the LLM already found is no product
page, such as a category).

Pages the parser cannot fill still go to the LLM tier. When enough of them have
gathered, a new version is generated. A version that fails its check is kept,
inactive, and the shop is not tried again for a week. The new version learns from,
and is checked on, only the pages the current one left to the LLM: the ones a
changed template broke. Specs are untrusted input: size limits, compiled
selectors, no code. Regular expressions are checked by their structure (no
repetition or alternation inside repetition, no back-references) and run with a
50 ms limit per match. On the held-out pages, a price's currency must match the
LLM's, and so must whether the product is unavailable (out of stock,
discontinued: such a price never counts) and whether the item is not new (used,
refurbished, damaged: flagged); a size must match in basis (product or package)
and net or gross. One version per shop is active: activation takes a lock per
shop, and the database refuses a second active version. A bare "$" or
"kr" is the shop's default currency when that uses the symbol. A page whose price
the LLM read as a unit, monthly, net or "from" price never passes (a parser reads
item prices only). A shop with such pages among the checked ones gets no automatic
activation: a person activates its parser after a test run. Panel: Parsers lists every version with its check. *Test* runs a version on
the shop's latest archived pages, beside the LLM's answers. Activating or
deactivating needs a recent sign-in. Switch: `site_parsers`.

### Price checks

Beyond the checks on each product's own offers (other shops, earlier visits), the
coordinator compares every product's price per kilogram and per cubic metre with
its category. It does this hourly, on a log scale, with a robust z-score (median
and MAD). A category with fewer than 20 comparable products uses its parent's
distribution. Products beyond `category_outlier_z` (3.5) get `category_outlier` on
their offers.

Flagged offers (far from other shops, two shops disagreeing, a jump between visits,
a category outlier) go to the LLM review: the default model in batches, at most
`price_review_per_round` (500) offers per batch, switch `price_review`. The model
sees the product, the extracted price and the page's price text. It answers valid,
placeholder, wrong price type (unit price, monthly rate, net, "from", rental),
pack mismatch, wrong product, or unclear. A rejection with medium or high
confidence sets `review_rejected`: the offer no longer counts for the product's
price. Panel: Price checks lists the flags and the latest verdicts with their
reasons. A rebuild drops reviews and outlier flags and compares prices without
them; both come back afterwards, the reviews from the ledger.

## Embeddings

Workers turn product texts (title, shop category path, size and mass) and taxonomy
categories (English and German path) into vectors locally: `embed` jobs with
multilingual-e5-small, pinned to one revision of its ONNX export. On the first embed
job a worker downloads the two model files (about 490 MB) from huggingface.co into
`<state dir>/models` and checks their SHA-256; afterwards it needs no network for
them. The model takes about 500 MB of RAM: nodes with less than 1.5 GB, or with
`POS_WORKER_EMBED=false`, take no embed jobs. Embed jobs come after fetches (lower
priority); at most 40 are queued at once, taxonomy categories first.

Vectors are stored per model key (`e5-small@<revision>/t1`): a new model or text
format gives a new key and everything is embedded again, never mixed. A product whose
text changed is embedded again too. A rebuild of the derived layer drops product
vectors (workers compute them again; the rebuild drill reports, not compares them);
taxonomy vectors stay.

## LLM (Claude API)

Only the coordinator calls the Claude API. Without a key the LLM stages stay idle;
everything else runs. Add the key to the coordinator's environment file and restart
it (the installer keeps the line on re-installs):

```bash
echo 'POS_ANTHROPIC_API_KEY=sk-ant-...' | sudo tee -a /etc/pos/coordinator.env
sudo chmod 600 /etc/pos/coordinator.env
```

* **Budget:** `llm_budget_hard_cap_usd` (default $10 a month), optional
  `llm_budget_soft_target_usd`, warning at `llm_budget_warn_fraction`. Spending is
  paced over the month (daily allowance = remaining budget / remaining days); low
  priority work waits when today is ahead of pace, everything waits at the cap. Set a
  spending limit on the API account as well: it is the second safety net.
  Each call reserves its estimate (prompt length plus the full `max_tokens`) in the
  ledger before it is made, and a batch reserves its estimate before it is submitted,
  so concurrent work never spends the same money twice; the actual cost replaces the
  estimate. A call whose outcome is unknown (timeout, lost connection) keeps its
  estimate, as does a batch submission that was interrupted.
* **Models** per role in the `llm_models` setting: Haiku 5.5 for bulk work
  (classification, reviews), Sonnet 5.5 where errors multiply (category mappings,
  parsers). Bulk work goes through the Message Batches API at half price; the
  coordinator collects finished batches every `POS_LLM_POLL_INTERVAL_S` (60 s).
* **Ledger:** every call is in `llm_call` (tokens, cost, model, stage, source; costs
  rounded up, an unknown model priced like the most expensive one). The same input is
  answered from the ledger instead of a second call. Answers must match a JSON schema;
  an invalid one is retried once, then kept as `invalid`. A refusal is kept as
  `refused`; Sonnet and Opus calls fall back to another model on the API's side first.
* **Thinking:** Haiku 5.5 and Sonnet 5.5 think before they answer (at effort `low`
  here), and thinking counts towards the answer's token limit, so every stage's limit
  leaves room for it (at least 1024 tokens). Only the tokens used are paid.
* **Asking again:** an answer that failed is kept per stage version, so it is not
  bought twice. A new version of a stage's prompt (e.g. `llm-classify-2`) asks again;
  answers that were fine come from the ledger at no cost. LLM extraction keeps its
  answers per fetch, so it asks an answer that was cut off (out of tokens) once more
  instead.
* `GET /api/admin/llm`: whether a key is set, the budget, spend per stage and model
  this month, and batches still running.

## Backups

* **Database role:** `pos-db-backup.timer` dumps the database nightly
  (custom format, newest 14 kept) to `<backup-dir>/database`;
  `pos-db-restore-test.timer` restores the newest dump into a scratch database
  every month. Check with `systemctl list-timers 'pos-*'` and
  `journalctl -u pos-db-backup`.
* **Before a coordinator update that migrates the database** the agent runs
  `pos-coordinator backup` into `<backup-dir>/coordinator` (newest 3 kept,
  `POS_AGENT_BACKUP_KEEP`; off with `POS_AGENT_BACKUP_BEFORE_MIGRATE=false` in the
  agent's environment). A release without migrations needs none: the agent asks
  the new release first (`pos-coordinator migrations-pending`).
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
* *LLM calls `invalid` with "no answer" or "no complete answer (stop_reason
  max_tokens)":* the answer ran out of tokens, usually while thinking. 0.1.2 and
  later leave room for it and ask such items again (on the DB host:
  `SELECT stage, output->>'detail', count(*) FROM llm_call WHERE output->>'status'
  = 'invalid' GROUP BY 1, 2;`).
* *The coordinator does not start; the agent logs "database 'pos' uses the encoding
  SQL_ASCII, not UTF8" (older releases: "cannot use a string pattern on a
  bytes-like object"):* the database was created on a system without a locale (a
  minimal container, `LANG` unset). Re-run `install.sh --role database` on the
  database host: it creates an empty database again as UTF8. A database that
  already holds data is left alone; move it with `pg_dump` / `pg_restore` into a
  new UTF8 database (`CREATE DATABASE pos OWNER pos ENCODING 'UTF8'
  LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' TEMPLATE template0`, then
  `CREATE EXTENSION vector`). The coordinator's agent retries on its own.
* *`/healthz` says `"raw_store": false`, log says "raw store ... is not this
  installation's store":* the raw store directory does not hold the marker
  (`.pos-rawstore`) the database recorded on first start, usually because the
  NAS is not mounted. Nothing is lost meanwhile: raw uploads are refused (nodes
  retry), fetch jobs stay queued and derivation waits. Mount the NAS; it
  recovers by itself. After *moving* the raw store on purpose, accept the new
  location once with `docker exec pos-coordinator-a pos-coordinator
  raw-store-adopt`.
