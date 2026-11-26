"""LLM cost ledger and monthly budget pacing.

The monthly hard cap is spent deliberately over the month:
daily allowance = remaining budget / remaining days (today included).
Low-priority LLM work is deferred when today's spend is ahead of that pace;
everything pauses at the hard cap. A spending limit on the API account itself
remains the second safety net.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator import runtime_settings
from pos_coordinator.models import LlmCall


class Priority(StrEnum):
    high = "high"  # e.g. parser generation, gold-standard second opinions
    normal = "normal"
    low = "low"  # e.g. bulk classification, background cross-checks


class BudgetState(StrEnum):
    ok = "ok"
    warn = "warn"  # past the warning fraction of the hard cap
    exhausted = "exhausted"


@dataclass(frozen=True)
class BudgetStatus:
    month_start: date
    hard_cap_usd: Decimal
    soft_target_usd: Decimal | None
    spent_month_usd: Decimal
    spent_today_usd: Decimal
    remaining_usd: Decimal
    remaining_days: int
    daily_allowance_usd: Decimal
    state: BudgetState

    @property
    def ahead_of_pace(self) -> bool:
        return self.spent_today_usd >= self.daily_allowance_usd

    def allows(self, priority: Priority) -> bool:
        if self.state == BudgetState.exhausted:
            return False
        if priority == Priority.high:
            return True
        if priority == Priority.low and self.ahead_of_pace:
            return False
        # normal work is deferred only once today's spend is well past pace
        return not (
            priority == Priority.normal and self.spent_today_usd >= 2 * self.daily_allowance_usd
        )


def compute_status(
    *,
    now: datetime,
    hard_cap: Decimal,
    soft_target: Decimal | None,
    warn_fraction: float,
    spent_month: Decimal,
    spent_today: Decimal,
) -> BudgetStatus:
    today = now.astimezone(UTC).date()
    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    remaining_days = days_in_month - today.day + 1
    target = min(hard_cap, soft_target) if soft_target is not None else hard_cap
    # Allowance for today: what is left of the target at the start of today, spread evenly.
    spent_before_today = spent_month - spent_today
    remaining_at_day_start = max(target - spent_before_today, Decimal(0))
    allowance = remaining_at_day_start / remaining_days
    if spent_month >= hard_cap:
        state = BudgetState.exhausted
    elif spent_month >= hard_cap * Decimal(str(warn_fraction)):
        state = BudgetState.warn
    else:
        state = BudgetState.ok
    return BudgetStatus(
        month_start=month_start,
        hard_cap_usd=hard_cap,
        soft_target_usd=soft_target,
        spent_month_usd=spent_month,
        spent_today_usd=spent_today,
        remaining_usd=max(hard_cap - spent_month, Decimal(0)),
        remaining_days=remaining_days,
        daily_allowance_usd=allowance.quantize(Decimal("0.000001")),
        state=state,
    )


async def status(session: AsyncSession, now: datetime | None = None) -> BudgetStatus:
    now = now or datetime.now(UTC)
    today_start = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), UTC)
    month_start = today_start.replace(day=1)
    month_end = (month_start + timedelta(days=32)).replace(day=1)
    spent_month, spent_today = (
        await session.execute(
            select(
                func.coalesce(func.sum(LlmCall.cost_usd), 0),
                func.coalesce(
                    func.sum(LlmCall.cost_usd).filter(LlmCall.created_at >= today_start), 0
                ),
            ).where(LlmCall.created_at >= month_start, LlmCall.created_at < month_end)
        )
    ).one()
    cfg = await runtime_settings.get_all(session)
    soft = cfg["llm_budget_soft_target_usd"]
    return compute_status(
        now=now,
        hard_cap=Decimal(str(cfg["llm_budget_hard_cap_usd"])),
        soft_target=Decimal(str(soft)) if soft is not None else None,
        warn_fraction=cfg["llm_budget_warn_fraction"],
        spent_month=Decimal(spent_month),
        spent_today=Decimal(spent_today),
    )
