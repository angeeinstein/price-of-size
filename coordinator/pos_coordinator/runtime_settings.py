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
    "rate_limit_scope": "per_egress_ip",  # or "global"
    "base_rate_rps": 0.2,  # 1 request per 5 s per domain
    "max_rate_rps": 2.0,  # adaptive ceiling per domain
    "max_total_rate_per_domain": None,  # optional hard cap across all egress groups
    "llm_budget_hard_cap_usd": 10.0,
    "llm_budget_soft_target_usd": None,
    "llm_budget_warn_fraction": 0.8,
    "html_archive_quota_gb": 100,
    "postgres_disk_budget_gb": 100,
    "coordinator.run_worker": False,
    "heartbeat_interval_s": 15.0,
    "job_lease_s": 300,
    "llm_models": {
        "default": "claude-haiku-5-5",
        "parser_generation": "claude-sonnet-5-5",
        "category_mapping": "claude-sonnet-5-5",
        "parser_chat": "claude-sonnet-5-5",
    },
}

VALIDATORS: dict[str, Any] = {
    "rate_limit_scope": lambda v: v in ("global", "per_egress_ip"),
    "base_rate_rps": lambda v: isinstance(v, int | float) and 0 < v <= 10,
    "max_rate_rps": lambda v: isinstance(v, int | float) and 0 < v <= 50,
    "llm_budget_hard_cap_usd": lambda v: isinstance(v, int | float) and v >= 0,
    "heartbeat_interval_s": lambda v: isinstance(v, int | float) and 2 <= v <= 600,
    "job_lease_s": lambda v: isinstance(v, int) and 30 <= v <= 86400,
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
