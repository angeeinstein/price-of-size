"""Runtime switches stored in the `setting` table, editable in the control panel.

Every key has a typed default here; the table only holds overrides, so a fresh
database works and new switches need no migration.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.models import Setting

DEFAULTS: dict[str, Any] = {
    "respect_robots_txt": True,
    # Block policy (per-domain overrides under the same names in domain overrides):
    # challenge pages (CAPTCHA, bot wall) in a row before a domain is blocked, the pause
    # after the first one, 403s in a row that count as a block, and after how many hours
    # a blocked domain is tried again (None: stays blocked until unblocked in the panel).
    "block_challenge_run": 2,
    "block_suspect_pause_s": 3600,
    "block_forbidden_run": 5,
    "block_retry_after_h": None,
    "rate_limit_scope": "per_egress_ip",  # or "global"
    "base_rate_rps": 0.2,  # 1 request per 5 s per domain
    "max_rate_rps": 2.0,  # adaptive ceiling per domain
    "max_total_rate_per_domain": None,  # optional hard cap across all egress groups
    "llm_budget_hard_cap_usd": 10.0,
    "llm_budget_soft_target_usd": None,
    "llm_budget_warn_fraction": 0.8,
    "html_archive_quota_gb": 100,
    "html_keep_first_pages": 500,  # always archive the first pages of every domain
    "html_keep_sample_fraction": 0.02,  # ...and a random sample of all others
    # Identifies the crawler honestly; robots.txt rules for this product token apply.
    "user_agent": "PriceOfSizeBot/0.1",
    "postgres_disk_budget_gb": 100,
    "coordinator.run_worker": False,
    # Release the coordinator's own agent should run (None = stay on the current one).
    "coordinator.target_version": None,
    "heartbeat_interval_s": 15.0,
    "job_lease_s": 300,
    "job_retention_days": 14,  # finished job rows (results and raw evidence stay)
    "failed_job_retention_days": 60,
    # Classification. k-NN guesses at or above this vote share are kept; products
    # without a classification or with a weaker one than `classify_llm_below_confidence`
    # go to the LLM (a batch of at most `classify_llm_per_round` at a time). Shop
    # categories with at least `category_mapping_min_products` products get LLM
    # mapping proposals. Bumping the generation runs every classifier again.
    "classify_knn_min_confidence": 0.8,
    "classify_llm": True,
    "classify_llm_below_confidence": 0.8,
    "classify_llm_per_round": 1000,
    "classify_llm_sample_fraction": 0.02,  # labelled by the LLM whatever their tier
    "category_mapping_proposals": True,
    "category_mapping_min_products": 3,
    "classification_generation": 0,
    # Price validity: robust z-score beyond which a product's price per kg or m³ is a
    # category outlier; flagged offers go to the LLM review (at most
    # `price_review_per_round` per batch).
    "category_outlier_z": 3.5,
    "price_review": True,
    "price_review_per_round": 500,
    # Tier-4 extraction: pages the other tiers left without price or sizes are read by
    # the LLM, at most `llm_extraction_per_round` per batch.
    "llm_extraction": True,
    "llm_extraction_per_round": 500,
    "llm_models": {
        "default": "claude-haiku-5-5",
        "parser_generation": "claude-sonnet-5-5",
        "category_mapping": "claude-sonnet-5-5",
        "parser_chat": "claude-sonnet-5-5",
    },
}


def _fraction(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and 0 <= v <= 1


def _int_in(lo: int, hi: int):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


BLOCK_VALIDATORS: dict[str, Any] = {
    "block_challenge_run": _int_in(1, 50),
    "block_suspect_pause_s": _int_in(0, 7 * 86400),
    "block_forbidden_run": _int_in(1, 1000),
    "block_retry_after_h": lambda v: v is None or _int_in(1, 24 * 365)(v),
}
BLOCK_KEYS = tuple(BLOCK_VALIDATORS)

VALIDATORS: dict[str, Any] = {
    "rate_limit_scope": lambda v: v in ("global", "per_egress_ip"),
    "base_rate_rps": lambda v: isinstance(v, int | float) and 0 < v <= 10,
    "max_rate_rps": lambda v: isinstance(v, int | float) and 0 < v <= 50,
    "llm_budget_hard_cap_usd": lambda v: isinstance(v, int | float) and v >= 0,
    "heartbeat_interval_s": lambda v: isinstance(v, int | float) and 2 <= v <= 600,
    "job_lease_s": lambda v: isinstance(v, int) and 30 <= v <= 86400,
    "job_retention_days": lambda v: isinstance(v, int) and 1 <= v <= 3650,
    "failed_job_retention_days": lambda v: isinstance(v, int) and 1 <= v <= 3650,
    "coordinator.target_version": lambda v: v is None or isinstance(v, str),
    "max_total_rate_per_domain": lambda v: (
        v is None or (isinstance(v, int | float) and 0 < v <= 50)
    ),
    "respect_robots_txt": lambda v: isinstance(v, bool),
    **BLOCK_VALIDATORS,
    "user_agent": lambda v: isinstance(v, str) and 3 <= len(v) <= 200,
    "html_keep_sample_fraction": lambda v: isinstance(v, int | float) and 0 <= v <= 1,
    "classify_knn_min_confidence": lambda v: _fraction(v),
    "classify_llm": lambda v: isinstance(v, bool),
    "classify_llm_below_confidence": lambda v: _fraction(v),
    "classify_llm_per_round": _int_in(1, 5_000),
    "classify_llm_sample_fraction": lambda v: _fraction(v),
    "category_mapping_proposals": lambda v: isinstance(v, bool),
    "category_mapping_min_products": _int_in(1, 1_000_000),
    "classification_generation": _int_in(0, 1_000_000),
    "category_outlier_z": lambda v: (
        isinstance(v, int | float) and not isinstance(v, bool) and 1 <= v <= 100
    ),
    "price_review": lambda v: isinstance(v, bool),
    "price_review_per_round": _int_in(1, 5_000),
    "llm_extraction": lambda v: isinstance(v, bool),
    "llm_extraction_per_round": _int_in(1, 5_000),
}


class UnknownSetting(KeyError):
    pass


class InvalidSetting(ValueError):
    pass


async def get_all(session: AsyncSession) -> dict[str, Any]:
    values = dict(DEFAULTS)
    for s in (await session.execute(select(Setting))).scalars():
        if s.key in DEFAULTS:
            values[s.key] = s.value
    return values


async def get(session: AsyncSession, key: str) -> Any:
    if key not in DEFAULTS:
        raise UnknownSetting(key)
    row = await session.get(Setting, key)
    return DEFAULTS[key] if row is None else row.value


async def put(session: AsyncSession, key: str, value: Any, actor: str) -> None:
    if key not in DEFAULTS:
        raise UnknownSetting(key)
    check = VALIDATORS.get(key)
    if check is not None and not check(value):
        raise InvalidSetting(f"invalid value for {key}: {value!r}")
    stmt = pg_insert(Setting).values(key=key, value=value, updated_by=actor)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": stmt.excluded.value, "updated_by": actor, "updated_at": func.now()},
    )
    await session.execute(stmt)
