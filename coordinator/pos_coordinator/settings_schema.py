"""How the control panel shows each runtime setting: group, label, help, input type,
range and unit. The panel builds its settings form from this; `runtime_settings`
still checks every value (tests keep both in step).

Kinds: bool, int, number, percent (stored as a fraction 0..1, shown as %), enum,
string, models (role -> model), generation (a counter with a "run again" button),
managed (set elsewhere in the panel; read-only here). `nullable` fields have an
"off" state whose meaning is `null_label`.
"""

from typing import Any

from pos_coordinator.llm.pricing import PRICES
from pos_coordinator.runtime_settings import DEFAULTS


def _f(key: str, label: str, help: str, kind: str, **opts: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "help": help, "kind": kind, **opts}


GROUPS: list[dict[str, Any]] = [
    {
        "title": "Crawling",
        "subtitle": "How politely and how fast shops are fetched",
        "fields": [
            _f(
                "respect_robots_txt",
                "Respect robots.txt",
                "Follow each site's robots.txt rules. It is always fetched and stored either way.",
                "bool",
            ),
            _f(
                "user_agent",
                "User agent",
                "How the crawler introduces itself to shops. "
                "Include a way to contact you, e.g. a URL or an e-mail address.",
                "string",
                min_length=3,
                max_length=200,
            ),
            _f(
                "base_rate_rps",
                "Start rate per domain",
                "Requests per second a domain "
                "starts at (0.2 = one every 5 seconds). The rate rises while the site "
                "answers well and falls when it slows down or refuses.",
                "number",
                min=0.01,
                max=10,
                step=0.01,
                unit="req/s",
            ),
            _f(
                "max_rate_rps",
                "Highest rate per domain",
                "Ceiling the adaptive rate never exceeds.",
                "number",
                min=0.01,
                max=50,
                step=0.01,
                unit="req/s",
            ),
            _f(
                "rate_limit_scope",
                "Rate limit applies",
                "Per public IP: each egress group "
                "gets its own budget per domain. Global: one budget per domain for all "
                "nodes together.",
                "enum",
                options=[
                    {"value": "per_egress_ip", "label": "per public IP"},
                    {"value": "global", "label": "globally"},
                ],
            ),
            _f(
                "max_total_rate_per_domain",
                "Hard cap per domain",
                "Total requests per second to one domain across all public IPs.",
                "number",
                min=0.01,
                max=50,
                step=0.01,
                unit="req/s",
                nullable=True,
                null_label="no cap",
            ),
        ],
    },
    {
        "title": "Blocking",
        "subtitle": "When a site counts as blocking the crawler (domains can override these)",
        "fields": [
            _f(
                "block_challenge_run",
                "Challenge pages before a block",
                "CAPTCHA or bot-wall pages in a row before the domain is blocked.",
                "int",
                min=1,
                max=50,
            ),
            _f(
                "block_suspect_pause_s",
                "Pause after a challenge page",
                "How long a domain rests after its first challenge page.",
                "int",
                min=0,
                max=7 * 86400,
                unit="s",
            ),
            _f(
                "block_forbidden_run",
                "403 answers before a block",
                "HTTP 403 answers in a row that count as a block.",
                "int",
                min=1,
                max=1000,
            ),
            _f(
                "block_retry_after_h",
                "Try a blocked domain again after",
                "Hours until a blocked domain is tried again.",
                "int",
                min=1,
                max=24 * 365,
                unit="h",
                nullable=True,
                null_label="only when unblocked by hand",
            ),
        ],
    },
    {
        "title": "LLM budget and models",
        "subtitle": "Claude API spending and which model does what",
        "fields": [
            _f(
                "llm_budget_hard_cap_usd",
                "Monthly cap",
                "LLM work stops for the rest of the "
                "month when spending reaches this. Spending is spread over the month.",
                "number",
                min=0,
                step=0.5,
                unit="USD",
            ),
            _f(
                "llm_budget_soft_target_usd",
                "Monthly target",
                "Optional lower amount to pace daily spending by instead of the cap; "
                "the cap still stops all work.",
                "number",
                min=0,
                step=0.5,
                unit="USD",
                nullable=True,
                null_label="none",
            ),
            _f(
                "llm_budget_warn_fraction",
                "Warn at",
                "Share of the cap at which the panel warns.",
                "percent",
            ),
            _f(
                "llm_models",
                "Models",
                "Which Claude model each kind of work uses. Bulk work: classifying "
                "products, reviewing prices, reading pages. Prices are per million "
                "tokens; work sent as batches costs half.",
                "models",
                roles=[
                    {"role": "default", "label": "Bulk work"},
                    {"role": "category_mapping", "label": "Category mappings"},
                    {"role": "parser_generation", "label": "Writing site parsers"},
                ],
            ),
        ],
    },
    {
        "title": "Extraction and site parsers",
        "subtitle": "Reading pages the built-in parsers could not",
        "fields": [
            _f(
                "llm_extraction",
                "LLM reads pages without price or sizes",
                "Pages the built-in parsers left without a price or sizes go to the LLM.",
                "bool",
            ),
            _f(
                "llm_extraction_per_round",
                "Pages per batch",
                "Most pages sent to the LLM in one batch.",
                "int",
                min=1,
                max=5000,
            ),
            _f(
                "site_parsers",
                "Write site parsers",
                "Once enough pages of a site were read "
                "by the LLM, it writes a parser for that site, so later pages need no LLM.",
                "bool",
            ),
            _f(
                "parser_min_pages",
                "Pages before a parser is written",
                "LLM-read pages of a site needed first.",
                "int",
                min=7,
                max=1000,
            ),
            _f(
                "parser_min_score",
                "Score to activate",
                "Share of held-out pages a new parser must read like the LLM did.",
                "percent",
            ),
            _f(
                "parser_auto_activate",
                "Activate good parsers automatically",
                "Otherwise they wait on the Parsers page.",
                "bool",
            ),
        ],
    },
    {
        "title": "Classification",
        "subtitle": "Sorting products into the product taxonomy",
        "fields": [
            _f(
                "classify_knn_min_confidence",
                "Keep a nearest-neighbour guess from",
                "Vote share among similar, already classified products.",
                "percent",
            ),
            _f(
                "classify_llm",
                "LLM classifies uncertain products",
                "Products without a confident classification go to the LLM.",
                "bool",
            ),
            _f(
                "classify_llm_below_confidence",
                "Ask the LLM below",
                "Classifications weaker than this are checked by the LLM.",
                "percent",
            ),
            _f(
                "classify_llm_per_round",
                "Products per batch",
                "Most products sent to the LLM in one batch.",
                "int",
                min=1,
                max=5000,
            ),
            _f(
                "classify_llm_sample_fraction",
                "Spot checks",
                "Share of all products the "
                "LLM also labels, whatever their tier, to measure the other tiers.",
                "percent",
            ),
            _f(
                "category_mapping_proposals",
                "Propose category mappings",
                "The LLM "
                "proposes how a shop's own categories map onto the taxonomy, for review on "
                "the Categories page.",
                "bool",
            ),
            _f(
                "category_mapping_min_products",
                "Products before a proposal",
                "A shop category needs this many products first.",
                "int",
                min=1,
                max=1_000_000,
            ),
            _f(
                "classification_generation",
                "Classification run",
                "Run all classifiers "
                "again over every product, e.g. after changing the settings above.",
                "generation",
            ),
        ],
    },
    {
        "title": "Price checks",
        "subtitle": "Catching prices that cannot be right",
        "fields": [
            _f(
                "category_outlier_z",
                "Outlier threshold",
                "Robust z-score of the price per "
                "kg or m³ within its category beyond which an offer is flagged.",
                "number",
                min=1,
                max=100,
                step=0.1,
            ),
            _f(
                "price_review",
                "LLM reviews flagged prices",
                "A rejected offer no longer counts towards the product's price.",
                "bool",
            ),
            _f(
                "price_review_per_round",
                "Offers per batch",
                "Most offers sent to the LLM in one batch.",
                "int",
                min=1,
                max=5000,
            ),
        ],
    },
    {
        "title": "Storage and retention",
        "subtitle": "What is kept, and for how long",
        "fields": [
            _f(
                "html_archive_quota_gb",
                "HTML archive size",
                "Until the archive reaches this size the full HTML of every page is kept; "
                "after that only the pages below. Keep it well under the coordinator's "
                "free disk space.",
                "number",
                min=0,
                step=1,
                unit="GB",
            ),
            _f(
                "html_keep_first_pages",
                "Always archive the first pages",
                "Full HTML of the first pages of every domain is kept even above the "
                "archive size, for building and testing parsers.",
                "int",
                min=0,
                max=1_000_000,
                unit="pages",
            ),
            _f(
                "html_keep_sample_fraction",
                "Archive a sample of the rest",
                "Random share of the other pages whose full HTML is kept even above the "
                "archive size.",
                "percent",
            ),
            _f(
                "job_retention_days",
                "Keep finished jobs",
                "Job rows only; results and raw evidence are kept regardless.",
                "int",
                min=1,
                max=3650,
                unit="days",
            ),
            _f(
                "failed_job_retention_days",
                "Keep failed jobs",
                "",
                "int",
                min=1,
                max=3650,
                unit="days",
            ),
        ],
    },
    {
        "title": "Nodes",
        "subtitle": "How the coordinator talks to nodes",
        "fields": [
            _f(
                "heartbeat_interval_s",
                "Heartbeat interval",
                "How often nodes report in.",
                "number",
                min=2,
                max=600,
                step=1,
                unit="s",
            ),
            _f(
                "job_lease_s",
                "Job lease",
                "A job a node took and did not report back within "
                "this time is handed to another node.",
                "int",
                min=30,
                max=86400,
                unit="s",
            ),
            _f(
                "coordinator.target_version",
                "Coordinator release",
                "Set on the Releases page.",
                "managed",
            ),
        ],
    },
]

# In the database and the JSON view, but nothing reads them yet.
UNUSED = {
    "postgres_disk_budget_gb": "not used yet",
    "coordinator.run_worker": "not used yet: the coordinator never runs a worker",
}


def _model_label(model_id: str) -> str:
    """claude-sonnet-5-5 -> Sonnet 5.5"""
    family, *version = model_id.removeprefix("claude-").split("-")
    return f"{family.capitalize()} {'.'.join(version)}".strip()


def schema() -> dict[str, Any]:
    return {
        "groups": GROUPS,
        "defaults": DEFAULTS,
        # cheapest first; prices per million tokens (batch work costs half)
        "models": [
            {
                "id": m,
                "label": _model_label(m),
                "input_usd": float(p.input),
                "output_usd": float(p.output),
            }
            for m, p in sorted(PRICES.items(), key=lambda mp: (mp[1].output, mp[0]))
        ],
        "unused": UNUSED,
    }
