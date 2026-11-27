"""Prices per million tokens (USD), for the cost ledger.

The ledger is what the monthly budget is checked against, so an unknown model is
priced like the most expensive known one rather than as free. Prices change: keep
this table in step with https://platform.claude.com/docs/en/about-claude/pricing.
"""

from dataclasses import dataclass
from decimal import ROUND_UP, Decimal

M = Decimal(1_000_000)
BATCH_FACTOR = Decimal("0.5")  # Message Batches: half price on all tokens
CACHE_WRITE_FACTOR = Decimal("1.25")  # 5-minute cache writes
CACHE_READ_FACTOR = Decimal("0.1")


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    # Some models cost more above a prompt size (Haiku 5.5 beyond 100k tokens).
    long_threshold: int | None = None
    long_input: Decimal | None = None
    long_output: Decimal | None = None


PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(
        Decimal("0.10"), Decimal("0.50"), 100_000, Decimal("0.50"), Decimal("2.50")
    ),
    "claude-sonnet-5-5": Price(Decimal("2.00"), Decimal("10.00")),
    "claude-sonnet-5": Price(Decimal("2.00"), Decimal("10.00")),
    "claude-opus-5-5": Price(Decimal("4.00"), Decimal("20.00")),
    "claude-opus-5": Price(Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": Price(Decimal("5.00"), Decimal("25.00")),
    "claude-fable-5-2": Price(Decimal("8.00"), Decimal("40.00")),
}
MOST_EXPENSIVE = max(PRICES.values(), key=lambda p: p.output)


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
) -> Decimal:
    p = PRICES.get(model, MOST_EXPENSIVE)
    prompt = input_tokens + cache_read_tokens + cache_write_tokens
    long = p.long_threshold is not None and prompt > p.long_threshold
    inp = p.long_input if long else p.input
    out = p.long_output if long else p.output
    total = (
        Decimal(input_tokens) * inp
        + Decimal(cache_write_tokens) * inp * CACHE_WRITE_FACTOR
        + Decimal(cache_read_tokens) * inp * CACHE_READ_FACTOR
        + Decimal(output_tokens) * out
    ) / M
    if batch:
        total *= BATCH_FACTOR
    # rounded up: the ledger must never under-count what the budget is checked against
    return total.quantize(Decimal("0.000001"), rounding=ROUND_UP)
