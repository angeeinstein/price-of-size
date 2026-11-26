from datetime import UTC, datetime
from decimal import Decimal as D

from pos_coordinator.budget import BudgetState, Priority, compute_status


def status(spent_month, spent_today, now=datetime(2026, 11, 21, 12, tzinfo=UTC), soft=None):
    return compute_status(
        now=now,
        hard_cap=D("10"),
        soft_target=soft,
        warn_fraction=0.8,
        spent_month=D(spent_month),
        spent_today=D(spent_today),
    )


def test_daily_allowance_spreads_remaining_budget():
    # Nov 21: 10 days left including today; 4 spent before today -> 6 / 10
    s = status("4", "0")
    assert s.remaining_days == 10
    assert s.daily_allowance_usd == D("0.6")
    assert s.state == BudgetState.ok


def test_low_priority_deferred_when_ahead_of_pace():
    s = status("4.7", "0.7")
    assert s.ahead_of_pace
    assert not s.allows(Priority.low)
    assert s.allows(Priority.normal)
    assert s.allows(Priority.high)


def test_warn_and_exhausted():
    assert status("8.5", "0").state == BudgetState.warn
    s = status("10", "0.1")
    assert s.state == BudgetState.exhausted
    assert not s.allows(Priority.high)
    assert s.remaining_usd == 0


def test_soft_target_paces_lower_than_cap():
    s = status("0", "0", now=datetime(2026, 11, 1, tzinfo=UTC), soft=D("6"))
    assert s.daily_allowance_usd == D("0.2")  # 6 / 30 days
