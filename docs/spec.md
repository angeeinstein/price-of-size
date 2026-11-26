# The Price of Size – Data Collection System Spec

## Goal and research questions

Build a self-hosted system that collects price, size and mass for as many purchasable things as possible, from a gravel sack to an aircraft, and stores them cleanly classified for statistical analysis.

Research questions:

1. Is there a relationship between price and size across many orders of magnitude, and what shape does it have?
2. Which size scalar explains price best: longest dimension, bounding-box volume, characteristic length or mass?
3. Is there a lower price bound that rises with size? Hypothesis: it rises roughly linearly with mass and with about volume^(2/3) (shell surface), while the upper bound stays open.
4. How much of the scatter does envelope density (mass / bounding-box volume) explain?
5. How do these relationships differ between categories (electronics, furniture, vehicles, buildings, bulk goods, land)?

The target volume is "as much as possible", tens of millions of records if feasible. 100 million is a rough order of magnitude, not a fixed goal.

## Guiding principles

Collecting is slow and expensive; interpreting is cheap and repeatable. The system must never force a re-crawl because a parser or classifier was wrong.

- **Separate collection from interpretation.** Fetching stores raw evidence. Extraction, normalization, price validation and classification are separate, re-runnable stages.
- **Keep raw evidence.** Store the relevant raw snippets (JSON-LD, spec tables, price block, breadcrumbs, title), compressed. Full HTML as far as storage allows (better to have than to need), see Database schema and storage.
- **Version everything.** Every derived value carries the stage version that produced it (`parser_version`, `classifier_version`, `validator_version`) and its method (rule, LLM, heuristic) plus confidence.
- **Never delete, only flag.** Questionable prices, dimensions and categories get flags. Filtering happens at analysis time.
- **Validate before scaling.** No long run before the pilot phase meets its error-rate targets (see Validation and QA).
- **Broad coverage over convenience.** Include bulk goods, fluids, land and buildings; tag them so they can be excluded later. Do not drop sources because they are inconvenient.
- **Record provenance.** Every value records source, URL, fetch time and how it was obtained, so source-specific bias can be measured.
- **Polite by design.** Slow, steady per-domain rates. Robots.txt handling is a setting (respect\_robots\_txt, default on); turning it off avoids sample bias from sites that disallow crawling. Blocks and refusals are logged per domain either way. No CAPTCHA solving or anti-bot evasion; actively blocked sources fall back to datasets.

## Hard-to-reverse decisions

Only what was never captured is truly lost; everything derived from captured evidence can be recomputed. The design therefore spends its care on the raw layer and treats everything else as rebuildable.

| Decision | Reversible later? | How the design handles it |
| --- | --- | --- |
| What raw evidence is captured per page | No: past pages and past prices cannot be re-fetched | Capture generously: JSON-LD, all spec tables, all offers, breadcrumbs, title, availability, image URLs, plus full HTML under the retention policy; capture fields even if unused today |
| Fetch timestamps and provenance | No | Stored on every fetch, immutable |
| Raw-layer schema | Hard at 100M rows | Minimal and generic: fetch id, URL, time, source, content type, blob reference, content hash |
| Identity of fetches, offers, products | Hard | UUIDv7 everywhere; raw fetches never change id; grouping into products is derived and can be redone |
| Reputation with shops | Partly: bans can last | Adaptive rates, cooldowns, block log |
| Derived schema (offers, measurements, products) | Yes | Rebuilt from raw instead of migrated; Alembic migrations |
| Taxonomy and classification | Yes | Separate versioned table; re-classified from stored text |
| Parsers, unit rules, price rules | Yes | Versioned; re-run over raw |
| Database location, node layout, control panel | Yes | Services behind the API; database in its own LXC |

**Rebuild drill** (part of the pilot gate): drop all derived tables, rebuild them from the raw layer, compare the result with the previous state and measure the time. LLM outputs are cached by input hash, so a rebuild repeats LLM calls only where prompts or models changed.

## Size definition

Store raw dimensions and mass; derive every size scalar at analysis time. Which scalar explains price best is itself a result.

**Stored per measurement set** (a record may have several, e.g. product and package):

- Three dimensions sorted descending: a ≥ b ≥ c, in metres
- Mass in kg
- Optional true volume (displacement, liquid content) in m³
- `basis`: `product` | `package` | `unknown`
- `basis_method`: `labelled` (shop says so) | `parser_rule` | `llm` | `heuristic`, plus confidence
- `net_or_gross` for mass when stated

**Derived scalars:**

| Scalar | Formula | Meaning |
| --- | --- | --- |
| Longest dimension | a | What people mean by "50 m big" |
| Bounding-box volume | a·b·c | Physical space taken |
| Characteristic length | (a·b·c)^(1/3) | Volume expressed in metres |
| Envelope density | mass / (a·b·c) | How "packed" a thing is |
| Shape factors | b/a, c/a | Flat, elongated or compact |

**Dimensionality flag:**

- `3d_object`: the normal case
- `2d_area` (land): area only; size scalar = √area
- `bulk_unit` (gravel, rice, water, fuel): no intrinsic size; store the purchase unit (25 kg sack, 1 m³, 1 l bottle) with its mass, volume and price

**Product vs. package.** Package dimensions can be larger or smaller than the product: packaging dominates small items (SD card, smartphone), while flat-pack furniture ships far smaller than the assembled product (an IKEA wardrobe). Never assume either direction. Store both when available. Heuristic flag: dimensions implausible for the category point to package. Where both exist, estimate a packaging factor per category; use it to correct package-only records or analyse them separately.

## Data sources

Volume comes from bulk datasets and many domains crawled in parallel; a single polite crawler on one domain yields only about 17,000 pages a day at one request per 5 s.

| Source | Type | Provides | Covers | Notes |
| --- | --- | --- | --- | --- |
| Amazon product metadata (McAuley Lab, UCSD) | Bulk dataset | Price, often dimensions and weight | Millions of consumer goods | Often shipping dimensions; preferred over scraping Amazon |
| IKEA | Scraper | Price, product and package dimensions, weights | Tealight to wardrobe | Very consistent; good for the pilot |
| Geizhals.at | Scraper | Specs with dimensions and weight, market prices, EAN | Electronics, appliances, tools | Good for the dense, complex end |
| Generic shop crawl | Scraper | schema.org/Product JSON-LD | Thousands of shops | One generic parser, no per-site code |
| Car catalogues (ADAC or similar) | Scraper | L×W×H, curb mass, list price | Hundreds of models | Clean data |
| Wikidata (SPARQL) / Wikipedia | API | Unit cost, dimensions, mass | Aircraft, ships, trains, satellites, buildings | Upper size range; mind cost year and currency |
| Eurostat Comext | Bulk dataset | Value, net mass, often unit count per product code | Everything traded, incl. bulk goods | No volume; unbiased by shop selection |
| Real estate listings (willhaben.at) | Scraper | Price, floor and plot area | Flats, houses, land | Land price distorts buildings; keep separate |
| Construction cost per m³ of built volume | Reference | Cost per m³ | Buildings without land | Industry key figure |
| Commodity prices (LME, building materials) | Reference | Price per kg or m³ | Gravel, concrete, steel, aluminium, copper | Lower-bound reference lines |

The source list is open: new adapters plug in without schema changes.

## System architecture

A control-only coordinator, a separate database container and any number of worker nodes. Workers reach the system only through the coordinator's API, locally or remotely through a Cloudflare Tunnel.

```
 worker nodes (local LXC, remote sites, spare PCs)
        │  HTTPS only (Cloudflare Tunnel + Access when remote)
        ▼
 Coordinator LXC: API, planner, LLM service, control panel ──► Claude API
        │                         │
        ▼                         ▼
 Database LXC:              NAS: raw store, HTML archive,
 PostgreSQL + pgvector      Parquet exports, DB backups
```

All data lives in one PostgreSQL database; workers never connect to it directly, and only the coordinator talks to the Claude API.

**Node roles (reference deployment, an example: three LXCs on a Proxmox host (coordinator, database, one worker); any Linux host or VM works):**

| Node | Runs | Resources |
| --- | --- | --- |
| Coordinator LXC | Coordinator API, planner, LLM service, control panel. Does not scrape by default (switch `coordinator.run_worker`). | 2–4 cores, \~1–2 GB RAM |
| Database LXC | PostgreSQL + pgvector, data directory on the host's local SSD | Small fixed RAM (\~2–4 GB); Postgres memory settings are fixed at start, so not dynamic |
| Worker LXC (Proxmox) | Fetching, browser rendering, extraction, local embeddings | Dynamic: e.g. 4 cores, RAM adjustable between \~2 and 8 GB |
| Remote nodes | Same worker software on any machine at another site, or on spare PCs and workstations | Whatever the machine offers |
| NAS | Raw store, HTML archive, Parquet exports, database backups (NFS/SMB). Not the live Postgres data directory: databases on network shares are slow and risk corruption. | Grows with data |

PostgreSQL gets its own LXC from the start, with its data directory on a separate volume (e.g. a Proxmox mount point) on local SSD. It can be sized, snapshotted and backed up independently, and the coordinator can be redeployed without touching data.

**Decision: one shared central database, reached only through the coordinator API.**

- Deduplication and global rate-limit bookkeeping need one view of all data.
- Workers stay stateless and disposable: a node can join or leave at any time without losing data.
- Workers speak HTTPS to the API (pull jobs, heartbeat, push results in batches, upload raw objects, download parser artifacts). Same path for local and remote nodes; the database is never exposed.
- A node that loses its connection buffers results locally (small SQLite spool) and flushes when the API is reachable again.

**Remote access.** The coordinator API and the control panel are published through a Cloudflare Tunnel and protected by Cloudflare Access: people log in through Access; nodes authenticate with Cloudflare Access service tokens plus a per-node API token issued by the coordinator at registration and revocable in the control panel. No inbound ports needed. Result batches stay below Cloudflare's request body limit (100 MB on the Free plan). WireGuard remains an alternative.

**Dynamic resource use.** Each node reads its own cgroup limits (CPU quota, memory limit) at start and every \~30 s and scales its concurrency up or down: HTTP slots, browser slots (roughly 1 per 1–1.5 GB free RAM and 1 core), embedding batch size. When RAM is reduced live in Proxmox, the node sheds browser slots first and lets running jobs finish instead of being OOM-killed. Worker LXCs get a low Proxmox CPU weight (`cpuunits`), so scraping uses idle CPU and yields when other services need it.

**Rate limits per egress IP (switch).** Each node's public egress IP is recorded at registration and refreshed (as seen by the coordinator). Nodes behind the same IP form an egress group automatically. Setting `rate_limit_scope`:

- `global`: one request budget per domain across all nodes
- `per_egress_ip` (default): each egress group gets its own per-domain budget

Plus an optional hard cap per domain across all groups (`max_total_rate_per_domain`), so no shop sees more than a set multiple of the base rate. Extra nodes at the same site add CPU and RAM only; nodes at other sites also add request capacity.

**Security.** Database and NAS reachable only inside the private network; node and user authentication as described in Authentication and security.

## Fetching and crawling

The fetcher is slow per domain and wide across domains; throughput comes from parallel domains, not from speed on one.

- **Domain leases.** A worker may fetch from a domain only while its egress group holds a lease for it (see Rate limits per egress IP). Start rate 1 request per 5 s per domain with jitter, then adaptive (next point); per-domain overrides in the control panel.
- **Adaptive back-off.** Per domain and egress group, the rate adapts like TCP congestion control: after a run of clean responses it rises in small steps (e.g. +10%), on 429, 503, timeouts, rising response times or a block page it halves and pauses, honouring Retry-After. Repeated throttling triggers longer cooldowns (minutes to hours). A configurable ceiling (default 2 requests/s per domain) caps tolerant sites. Every change is recorded in the domain state and shown in the control panel.
- **Plain HTTP first, browser second.** If a page yields no product data via HTTP, re-queue as `fetch_browser` (Playwright, headless Chromium). Remember per domain which mode works.
- **Discovery.** Seeds from sitemaps, category pages and pagination. Product URLs normalized and deduplicated before queueing.
- **Robots.txt** is fetched and stored for every domain and enforced unless respect\_robots\_txt is off.
- **Blocks.** CAPTCHAs, bot walls and hard refusals mark the domain `blocked` with reason and date; no evasion. The block log lets the analysis quantify the bias.
- **Re-fetch policy.** Optional second visit to a sample of products weeks later to build price history.
- **Storage per fetch:** URL, final URL, status, timestamp, fetch mode, response size, and the compressed raw snippets the extractor needs (JSON-LD, spec tables, price block, breadcrumbs, title). Full HTML is archived compressed according to the HTML retention policy (see Database schema and storage).

## Extraction

Extraction runs in tiers from cheapest to most expensive; each record stores which tier produced each field.

1. **Generic structured data.** schema.org `Product` / `Offer` in JSON-LD, Microdata or RDFa: name, brand, GTIN/EAN, price, currency, availability, itemCondition, weight, width/height/depth. One parser for all shops.
2. **LLM-generated per-site parsers.** For a domain where tier 1 is missing or incomplete, the coordinator sends 5–10 sample pages to the LLM, which writes selectors or a small parser. The parser is validated on a further held-out sample before activation, stored versioned, and then runs without the LLM. Re-generated automatically when its success rate on a domain drops.
3. **Spec text parsing.** Regex and rules for strings like "B×H×T: 120×80×45 cm, 12,5 kg", German and English labels, decimal comma vs. point, mm/cm/m/inch, g/kg/lb/oz, net/gross, product/package/shipping labels.
4. **LLM fallback on the full page.** For pages where tiers 1–3 fail or conflict: cleaned page content (scripts, CSS, navigation removed, about 1,500 tokens) goes to the LLM with a strict JSON output schema. Used generously within the budget.

Every extracted field records `method`, `confidence` and `parser_version`. Conflicting values from different tiers are kept, not overwritten.

## Normalization and deduplication

All quantities go to SI and all prices to EUR, while the original value, unit and currency stay stored.

- **Units:** metres, kilograms, cubic metres. Keep the original string and the parsed unit.
- **Currency:** convert with ECB reference rates for the fetch date. Store original amount, currency and rate used.
- **VAT:** flag net vs. gross; analysis uses gross consumer prices by default.
- **Inflation:** store the price date; for older reference data (aircraft unit costs, historic catalogues) store the cost year so it can be adjusted later.
- **Dimension ordering:** sort to a ≥ b ≥ c after conversion.
- **Deduplication:** an `offer` is one price at one source on one date; a `product` groups offers. Match by GTIN/EAN first, then by brand + model number, then by fuzzy title + specs (embedding similarity) with low confidence. One product can therefore have many offers, which also gives price spread across shops.

## Price validity

Prices are never taken blindly: each offer stores the raw price plus validity flags, and the canonical product price is the median of valid offers.

**Known traps to flag:**

| Trap | Examples | Detection |
| --- | --- | --- |
| Placeholder price | 0, 0.01, 1, 999.99, 9,999, 99,999 | Pattern list + availability |
| Not available | Out of stock, discontinued, stale price | schema.org `availability`, page text |
| Wrong price type | Unit price per 100 g or per litre, net B2B price, instalment "from €29/month", contract price (phone for €1 with plan), rental | Keywords: "/Monat", "zzgl. MwSt.", "pro kg", "ab" |
| Pack mismatch | Price for 10, dimensions for 1 (or the reverse) | Pack size from title and specs |
| Condition | Used, refurbished, B-stock, display item | schema.org `itemCondition`, keywords |
| Marketplace outliers | Third-party sellers with absurd prices | Cross-source comparison |
| Parse errors | "1.299" read as 1.299 instead of 1299; wrong currency | Plausibility vs. category, cross-source |

**Detection pipeline:**

1. Rule flags during extraction (table above).
2. Cross-source check: for products with several offers (via GTIN), offers far from the product median (e.g. factor > 3) are flagged. Strongest filter.
3. Category outliers: price per kg and per m³ compared with the category using a robust z-score (median and MAD).
4. Price history: jumps of an order of magnitude between visits are flagged.
5. LLM review for flagged cases only: classifies as valid price, placeholder or wrong price type.

The gold standard in the pilot also checks prices by hand, to measure how many bad prices pass the filters.

## Classification

Every product maps to one node of an existing hierarchical taxonomy, so the analysis can aggregate and weight at any level.

- **Target taxonomy:** Google Product Taxonomy (about 5,500 categories, hierarchical) as primary; optional cross-walk to GS1 GPC and HS codes (for Comext). Extended by a few top-level classes it lacks: vehicles and aircraft, buildings, land, bulk materials, fluids.
- **Extra attributes** beyond the category: dimensionality flag, new/used, consumer vs. B2B, bulk vs. discrete.

**Tiers, cheapest first:**

1. **Shop category mapping.** Breadcrumbs per shop are mapped once to the target taxonomy; the LLM creates the mapping table per shop, a human spot-checks it.
2. **Local embeddings + classifier.** A small local multilingual embedding model, by default multilingual-e5-small (384 dimensions) and compared with paraphrase-multilingual-MiniLM-L12-v2 in the pilot (CPU is enough, a few hundred MB of RAM, run as \`embed\` jobs on workers) turns title + breadcrumbs + key specs into a vector, so similar products lie close together; vectors stored in pgvector. Local because embedding every product through a paid API would not fit the budget at scale. A lightweight classifier is trained on LLM-labelled data (tens of thousands of items, stratified by source).
3. **LLM classification.** For low-confidence cases, and generously within budget: embeddings propose the top \~20 candidate categories, and the LLM picks one or answers "none". The full taxonomy is never put in the prompt.

Stored per record: category id, tier used, confidence, `classifier_version`. Re-classification runs over stored data whenever the taxonomy or the classifier changes.

## LLM usage and budget

The LLM budget is a configurable monthly amount (default $10); the system spends it deliberately over the month and stops cleanly when it is used up.

- **Default model:** Claude Haiku 5.5 via the Batch API (50% off). [Pricing](https://platform.claude.com/docs/en/about-claude/pricing): $0.10 / $0.50 per million input / output tokens up to 100k-token prompts; batch halves that; cache hits cost 0.1× input.
- **Stronger model where errors multiply:** Sonnet for generating per-site parsers and category mappings, and as second opinion on gold-standard and hard price/packaging cases. Few calls, high leverage.
- **Prompt caching** for the fixed instructions and JSON schemas; keep each prompt under 100k tokens.
- **Strict JSON output** with schema validation; invalid answers are retried once, then flagged.

**Rough cost per unit (Haiku 5.5, batch):**

| Use | Approx. cost |
| --- | --- |
| Classify 1 million products (\~300 tokens in, \~30 out) | \~$20–25 |
| Full-page extraction, \~1,500 tokens per page | \~$0.10 per 1,000 pages |
| Label 50,000 training items | \~$1 |
| Generate one per-site parser incl. iterations | cents |

**Budget control:**

- Every API call is logged in a cost ledger: tokens, cost, model, stage, source, job.
- Monthly budget set in the control panel: hard cap (default $10) and an optional lower soft target. Spending is paced over the month (daily allowance = remaining budget / remaining days); when ahead of pace, low-priority LLM jobs are deferred first. Warning at 80% of the hard cap, LLM stages pause at 100%; deferred jobs wait in the queue.
- As a second safety net, a spending limit or prepaid credit on the API account itself also caps costs.
- Long-running LLM work is spread over several months instead of exceeding a month's budget.

**Where extra budget helps most:** more direct LLM classification on shops with weak categories, LLM fallback whenever parsers are unsure (not only when they fail), continuous LLM cross-checks on about 0.5% of all records.

## Database schema and storage

PostgreSQL is the operational store; periodic Parquet exports analysed with DuckDB (or Polars) are the analysis store.

**Why PostgreSQL:** fixed fields in typed columns plus source-specific data in JSONB; pgvector for embeddings and similarity search in the same database; a job queue via `SKIP LOCKED` with no extra broker; partitioning handles 100M+ rows on one server. SQLite fails with many concurrent writers; MongoDB is convenient for collecting but awkward for analysis.

**Two layers.** The raw layer (fetches and their stored evidence) is append-only, minimal and generic, and its schema should practically never change. Everything else is the derived layer: rebuilt from raw by re-running the stages rather than migrated in place. Every row gets a UUIDv7 id; schema migrations run through Alembic from day one; large tables are partitioned (e.g. by source and month).

**Core tables (draft):**

| Table | Holds |
| --- | --- |
| `source` | Adapter name, type (dataset / API / scraper), config, status |
| `domain_state` | Rate limit, lease holder, fetch mode, block status and reason, robots.txt snapshot |
| `job` | Type, payload, status, attempts, worker, lease expiry, priority |
| `worker` | Node id, capabilities, cgroup limits, last heartbeat |
| `fetch` | URL, final URL, status, time, mode, pointer to raw snippet object |
| `offer` | Source, URL, date, raw price string, price, currency, EUR price, VAT flag, availability, condition, pack size, validity flags |
| `product` | Canonical product: GTIN, brand, model, title, canonical price, category |
| `measurement` | Product, basis (product / package / unknown), a/b/c, mass, true volume, method, confidence, versions |
| `classification` | Product, taxonomy node, tier, confidence, classifier version (history kept) |
| `embedding` | Product, model, vector |
| `parser` | Domain, version, code artifact, validation score, active flag |
| `llm_call` | Model, stage, tokens in/out, cost, job, timestamp (cost ledger); input hash and output kept as a cache, so rebuilds do not pay twice |
| `gold_label` | Hand-checked values per record for QA |
| `taxonomy_node` | Taxonomy tree incl. custom extensions |

**Storage estimate:** compressed raw snippets of a few KB per page, so 10 million pages ≈ tens of GB; raw HTML (\~100 KB per page) compresses well with zstd, especially with a per-domain dictionary because pages of one shop share templates: expect roughly 10–20 KB per page, i.e. \~100–200 GB per 10 million pages (to be measured in the pilot). Postgres holds only structured rows and vectors, roughly 3–5 KB per product including indexes, so \~50 GB per 10 million products (to be measured in the pilot). Postgres data lives on the host's local SSD; raw store, HTML archive and Parquet exports on the NAS; nightly database backups to the NAS.

**Keeping Postgres within its local SSD budget (configurable, default 100 GB; beyond that, offload):**

- Raw snippets and HTML never go into Postgres; they live on the NAS, Postgres stores only references.
- Vectors start as full-precision float32. Embeddings are derived data, so switching to 16-bit `halfvec` (half the size) later is a cheap, reversible step if space gets tight; check the effect on classification accuracy in the pilot first.
- Hot and cold data: Postgres is the working set. Finished partitions (e.g. older months per source) are exported to Parquet on the NAS, verified, then detached and dropped from Postgres. Analysis runs on Parquet with DuckDB anyway.
- Disk usage shown in the control panel with a warning threshold; at the threshold the planner exports and detaches the oldest finished partitions first.
- Fallback if the hot set must grow: an iSCSI block volume from the NAS (block storage, not NFS/SMB) as an extra Postgres tablespace, or an additional local disk. Both can be added later without changing the schema.

**HTML retention policy (better to have than to need):**

- Always kept: pages where extraction failed or conflicted, pages used to generate or validate a parser, the first \~500 pages of every new domain, and a random sample (\~2%).
- Everything else is kept while the HTML archive is below its storage quota, set in the control panel (default 100 GB).
- At the quota the system switches to sample-only for new pages. Existing archives are never deleted automatically; pruning is a manual action in the control panel.

## Control panel

A modern web app on the coordinator is the single place to watch, steer and inspect the whole system; it sits behind Cloudflare Access.

**Stack:** FastAPI backend (same service as the coordinator API), React + TypeScript (Vite), Tailwind + shadcn/ui, live updates over WebSocket or SSE, charts with ECharts or Recharts, light and dark mode.

| Page | Shows and does |
| --- | --- |
| Overview | Records per hour, totals by source and category, error and flag rates, budget pace, alerts |
| Nodes | Every node live: status, egress group, CPU and RAM use vs. cgroup limits, active jobs, throughput, last heartbeat; drain, pause, revoke token |
| Jobs and queue | Queue depth by type and priority, failures with stack traces, retry or cancel |
| Domains | Rate limit, lease holders, fetch mode (HTTP or browser), block status and reason, success rate over time; per-domain overrides |
| Sources and parsers | Adapters and per-site parsers with versions, code view with diffs, test run on sample URLs (extracted values beside the raw page), activate, deactivate, roll back |
| AI parser chat | Chat with Claude (Sonnet) scoped to one parser or source: reads the code and sample pages, proposes a patch, runs it on held-out samples and shows the results. A change becomes a new parser version that a person activates. |
| Data explorer | Search and filter products and offers, raw snippet beside extracted values and flags, quick log-log plots by category |
| QA | Gold-standard labelling UI; error rates per source, field and stage version |
| Costs | LLM cost ledger by stage, model and source; budget pace; monthly cap |
| Settings | All switches: `rate_limit_scope`, respect\_robots\_txt, per-domain rates and global cap, budget target and cap, HTML archive quota, `coordinator.run_worker`, model per stage |

AI chat usage counts against the same monthly LLM budget.

## Validation and QA

The long run starts only after a pilot has measured error rates per source and field and they are acceptable for the analysis (gate rule below).

**Pilot:** 10,000–50,000 records across all initial sources, then a gold standard of 500–1,000 records stratified by source and category, checked by hand (dimensions, units, product vs. package, mass, price, price type, category). A small review web UI shows the raw snippet beside the extracted values for fast checking.

**Gate rule: measure first, then judge against what the analysis needs.** Small errors (a few percent in a dimension) barely matter on a log scale spanning 10+ orders of magnitude. Gross errors do: unit mix-ups by ×10 to ×1000, package taken as product, placeholder prices. Envelopes are only as good as the gross-error rate: if 2% of records are garbage, a 1% quantile line describes the garbage.

- Gross-error rate per source must stay well below the lowest quantile used: e.g. < 0.5% for a 1% envelope, < 2% for a 5% envelope.
- Sources above that are fixed or excluded before scaling.
- 1,000 gold records measure a 1% rate only to about ±0.6 percentage points; the gold set grows for sources with borderline results.

**Automatic plausibility checks** (run on every record, also in the long run):

- Envelope density between \~1 and \~23,000 kg/m³ (osmium is 22,600); catches most mm/cm/m mix-ups
- Gross mass ≥ net mass. No rule between package and product dimensions (flat-pack ships smaller than assembled); the density check applies to each separately
- Price per kg within a factor of \~1,000 of the category median
- Dimensions plausible for the category (smartphone longest side 10–20 cm)

**Ongoing QA in the long run:** random samples (\~0.5% of records) re-checked by the LLM; dashboard shows error and flag rates per source, per stage version and over time; automatic alert when a domain's extraction success rate drops (site redesign).

## Analysis plan

The core exhibit is a log-log scatter of price against each size scalar, with envelopes from quantile regression rather than a single mean line.

- **Plots:** price vs. mass, vs. bounding-box volume, vs. longest dimension, vs. characteristic length; coloured by top-level category; density (hexbin) for large n.
- **Envelopes:** quantile regression at 1%, 5%, 50%, 95%, 99%. Hypotheses: lower envelope slope ≈ 1 in mass, ≈ 2/3 in volume; upper envelope weakly constrained.
- **Model:** log price = α + a·log V + b·log ρ + category effects; how much variance does envelope density explain?
- **Weighting:** results reported both unweighted and with equal weight per category (at several taxonomy levels), so over-represented categories such as electronics do not dominate.
- **Robustness:** leave-one-source-out (does an effect come from one shop?), product-only vs. package measurements, new vs. used, with and without bulk goods, land and buildings.
- **Side results:** packaging factor per category, price spread across shops for the same product, price per kg by category.
- **Reference lines:** commodity prices (gravel, concrete, steel) as the material floor.

## Implementation notes

One monorepo, containerised services, and tests built from real archived pages; the same worker image runs on every node.

- **Repo layout:** `coordinator/` (FastAPI API, planner, LLM service), `worker/`, `shared/` (schemas, unit and price rules, API client), `panel/` (React app), `migrations/` (Alembic), `deploy/`, `docs/`. A `CLAUDE.md` at the root links to this spec.
- **Deployment:** Database LXC: PostgreSQL installed natively (apt), no container. Coordinator and workers: Docker Compose, installed and driven entirely by the bootstrap script, so nobody has to operate Docker by hand; the LXCs need the nesting feature. Docker makes the A/B update slots and rollback simple and gives every node, including foreign machines and arm64, the same environment with Playwright and Chromium. One worker image for all nodes, versioned tags; Chromium containers get a larger shared-memory size (e.g. 1–2 GB /dev/shm) to avoid browser crashes. The packaging choice is reversible. A worker starts with only the coordinator URL and its credentials; everything else comes from the coordinator.
- **Configuration:** static settings in a config file, runtime switches (rates, budget, quotas, scopes) in the database and editable in the control panel. Secrets (Claude API key, Cloudflare service tokens, node tokens) only in environment files, never in the repo.
- **Tests:** unit tests for unit parsing, price rules and plausibility checks; golden-file tests per parser using archived pages from the HTML archive, so every parser change is tested against real pages; an end-to-end test with a local fake shop.
- **Observability:** structured JSON logs with job and node ids; metrics shown in the control panel; alerts through ntfy, Telegram or email for: node offline, domain blocked, error rate jump, budget threshold, disk threshold, backup failed.
- **Backups:** nightly Postgres backups (pg\_dump or pgBackRest) to the NAS, Proxmox snapshots of the LXCs, and a monthly automated restore test into a scratch container.
- **Upgrades:** coordinator and workers version their API; workers refuse jobs from an incompatible coordinator version and update by pulling the new image.

## Installation, updates and remote node management

Every role installs and updates with one command, and a node must run unattended for years at a place nobody visits again (e.g. a Raspberry Pi), steered entirely by the coordinator.

**Bootstrap script** (`install.sh` in the GitHub repo):

- One line on a fresh Ubuntu/Debian machine, e.g. `curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/install.sh | sudo bash`.
- Interactive menu with roles: **Node (default)**, Coordinator, Database, All-in-one (for testing). Asks only what the role needs (coordinator URL, enrollment code, storage paths).
- Fully non-interactive mode with flags for headless installs, e.g. `--role node --coordinator https://… --enroll <code>`.
- Installs Docker if missing and starts the node agent container (or the coordinator stack); Docker's restart policy brings everything back after a reboot or power loss. Idempotent: running it again repairs instead of breaking.
- The same script updates (`install.sh --update`) and uninstalls (`--uninstall`, keeping data unless told otherwise).
- Supports amd64 and arm64 (e.g. Raspberry Pi) via multi-arch images. Without the script, a node on any machine with Docker (Linux, Windows or macOS with Docker Desktop, NAS container managers) is a single docker run of the agent container with an enrollment code.
- Installs pinned release versions and verifies image digests, not whatever is on `main` at that moment.

**Enrollment:** the control panel creates a one-time enrollment code; the node exchanges it for its node token and the Cloudflare service token. Alternatively, a new node shows a pairing code that is approved in the panel. Nodes connect outbound only, so they work behind NAT or CGNAT with no port forwarding.

**Updates from the control panel:**

- Panel shows the version of the coordinator and every node, with available releases.
- Rollout is staged: one canary node first, the rest after it reports healthy.
- Each node has a small, rarely changing **updater agent** separate from the worker. It pulls the new image into the inactive of two slots (A/B), starts it beside the running version, runs health checks and only then switches. The previous version stays in the other slot until the next update, so a rollback is a single switch. The updater is itself a small container, the node agent, with access to the Docker API (Docker socket). The user starts only the agent; the agent pulls and runs the worker containers in their A/B slots. Agent and worker update each other, never both at once: the agent replaces the worker; the worker recreates the agent with a new version and restores the old one if its health check fails. Docker's restart policy restarts anything that crashes and brings everything back after a reboot. For people who do not want to give a container access to their Docker socket, a simple mode runs a single worker container without the agent; it is updated by hand, and the coordinator refuses work from versions that are too old. There is always one healthy component left to roll back the other, so a bad release can never cut off a remote node.
- The coordinator uses the same agent and A/B pattern (blue-green): the new version starts beside the old one, is health-checked on an internal port, then a reverse proxy in the stack (e.g. Caddy) switches traffic; nodes buffer results during the few seconds of switch-over. Database migrations follow expand/contract: a release only adds (tables, columns, indexes) so the old version still works on the new schema and rollback stays a single switch; removals happen in a later release once the new version is confirmed. A backup is taken before every migration, migrations run inside a transaction (PostgreSQL supports transactional DDL), and large data conversions run as background jobs rather than during the switch. PostgreSQL major-version upgrades are rare, manual and handled by the bootstrap script, not the panel.

**Remote operation from the coordinator:** parsers, crawl targets, rates, limits, schedules and settings all come from the coordinator; a node needs no local configuration beyond its credentials. From the panel: drain, pause, restart, update, fetch recent logs, change limits.

**Node dashboard** (small local web UI, LAN only, password set at install):

- Status: connection to the coordinator, version, current jobs, throughput, CPU, RAM, disk and bandwidth use, recent errors and logs.
- Local controls: pause and resume, maximum CPU, RAM and bandwidth, quiet hours (e.g. no scraping during the evening). Local limits always win over the coordinator: whoever owns the machine and the internet connection decides.

**Robustness for unattended nodes:** offline buffering with a size limit; automatic restart; watchdog; log rotation; on Raspberry Pis, minimal writes to the SD card (buffers in RAM or on a USB SSD) to avoid card wear.

**Control channel: desired state over heartbeat.**

- Every node sends a heartbeat every \~15 s (configurable): version, actual state (running, paused, draining), resource use, queue status, results of earlier commands.
- The response carries the **desired state** (run or pause, target version, limits, active parsers with versions, assigned domains and rates) plus a list of **one-off commands** with ids (restart, fetch logs, run diagnostics).
- The node reconciles: it compares desired with actual state and fixes the difference itself. A lost message does no harm, because the next heartbeat carries the full desired state again. Commands are acknowledged by id and run at most once.
- Live channel: each node keeps an outbound WebSocket open to the coordinator (works through Cloudflare; no tunnel or open port per node). Commands arrive within a second, and the panel streams a node's live stats and logs, which feels like direct control. Heartbeat polling always remains the fallback. An optional remote diagnostics shell exists for owned nodes only, off by default, never for community nodes.
- Jobs travel separately: the node pulls batches of jobs when it has free capacity and returns results in batches.

## Authentication and security

Cloudflare Access is an outer layer only; the coordinator authenticates every person and every node itself, so the system stays safe if Access is ever removed or the project opens to volunteers.

**Nodes:**

- At enrollment the node generates an Ed25519 key pair; the private key never leaves the node. The coordinator stores the public key with the node record.
- To call the API, the node signs a challenge and receives a short-lived access token (e.g. 15 minutes). No long-lived secret travels over the network after enrollment.
- Every node can be revoked instantly in the panel; revocation also invalidates its current token.
- Per-node rate limits, payload size limits and strict schema validation of every result. Authentication only proves who sent data, not that it is correct: community results are additionally verified (see Future: community nodes).

**Code sent to nodes:**

- Parser artifacts and release images are signed by the coordinator's signing key; nodes pin that public key at enrollment and refuse anything unsigned or wrongly signed.
- Parsers are preferably declarative: LLM-generated extraction specs (CSS/XPath selectors, regexes, unit hints) run by a trusted engine, which is both safe and fast. Code parsers are the fallback and run in a pool of long-lived sandboxed processes (no network, no file system beyond their input). The cost is a one-time process start, not per page, so the overhead is negligible next to fetching and rendering.

**People (control panel):**

- Own user accounts with passkeys (WebAuthn) or password + TOTP; passwords hashed with Argon2.
- Roles: admin (updates, parser activation, settings, node enrollment) and viewer.
- Secure, HTTP-only session cookies, CSRF protection, login rate limiting, audit log of every admin action.
- Sensitive actions (activate parser, roll out update, revoke node, change budget) require a recent re-authentication.

## Future: community nodes

Volunteers could later run worker nodes; the architecture allows it, but their nodes must be treated as untrusted. This is optional and possibly never needed: no performance or functionality of the own deployment may be traded for it, and it is built only if it actually happens.

- **Trust levels per node:** `owned` (own machines) and `community`. Community results are accepted only after verification.
- **Verification:** a share of community results (and all suspicious ones) is re-fetched by an owned node and compared; nodes with mismatches are demoted or revoked automatically.
- **Least privilege:** community nodes receive only fetch and extract jobs with signed parser artifacts; no access to the data explorer, other nodes' results or the LLM.
- **Self-service onboarding:** registration link, per-node token, per-node quotas and a public status page.
- **Transparency for volunteers:** their IP does the fetching, so they choose which domains or categories they help with and can pause at any time.

## Phases and roadmap

Build the full pipeline small first, prove it on three sources, then scale sources and nodes; each phase ends at a gate.

1. **Foundation.** Schema, job queue, coordinator API, database LXC and one worker LXC, cgroup-aware concurrency, cost ledger, control panel skeleton, bootstrap script for all roles, updater agent. Gate: jobs run end to end on dummy data; a second worker joins and leaves without data loss.
2. **MVP sources.** IKEA, Geizhals and Wikidata adapters; tier-1 and tier-3 extraction; normalization; shop-category mapping. Gate: first log-log plot from real data.
3. **Classification and LLM.** Embeddings, LLM labelling, classifier, LLM-generated per-site parsers, price validity pipeline. Gate: all stages versioned and re-runnable over stored raw data.
4. **Pilot and gold standard.** 10,000–50,000 records, 500–1,000 checked by hand, review UI. Gate: error rates acceptable per source and field (gate rule), and the rebuild drill succeeds.
5. **Scale-up.** Generic JSON-LD crawl, Amazon dataset import, Comext, car catalogues, real estate, reference data; extra worker nodes as needed. Gate: ongoing QA stable over several weeks.
6. **Long run and analysis.** Continuous collection within monthly LLM budget; regular Parquet exports; analysis notebooks.

## Open questions

- [ ] Per-domain overrides for large shops, once the adaptive rates of the pilot are known.
- [ ] Community nodes (future): when to open up, and how much verification is enough.
