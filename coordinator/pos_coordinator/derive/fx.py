"""ECB euro reference rates.

Prices are converted with the rate of the fetch date; on weekends and holidays
the ECB publishes nothing, so the latest rate before it is used (up to a week
back). The original amount, currency and the rate used are always stored.
"""

import csv
import io
import logging
import zipfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.models import FxRate

log = logging.getLogger(__name__)
ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
MAX_STALENESS = timedelta(days=7)


def parse_ecb_csv(text: str) -> list[dict]:
    """eurofxref-hist.csv: Date,USD,JPY,...; 'N/A' for currencies not quoted that day."""
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        day = date.fromisoformat(rec.pop("Date").strip())
        for cur, val in rec.items():
            if not cur or not cur.strip():
                continue
            try:
                rate = Decimal(val.strip())
            except (InvalidOperation, AttributeError):
                continue  # N/A, empty
            rows.append({"day": day, "currency": cur.strip().upper(), "rate_per_eur": rate})
    return rows


async def store(session: AsyncSession, rows: list[dict], batch: int = 5000) -> int:
    for i in range(0, len(rows), batch):
        stmt = pg_insert(FxRate).values(rows[i : i + batch])
        stmt = stmt.on_conflict_do_update(
            index_elements=["day", "currency"], set_={"rate_per_eur": stmt.excluded.rate_per_eur}
        )
        await session.execute(stmt)
    return len(rows)


async def refresh_from_ecb(session: AsyncSession, client: httpx.AsyncClient | None = None) -> int:
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        r = await client.get(ECB_HIST_URL)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            text = z.read(z.namelist()[0]).decode("utf-8")
    finally:
        if own:
            await client.aclose()
    return await store(session, parse_ecb_csv(text))


class RateCache:
    """Rates for one derive batch: few currencies, many lookups."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._cache: dict[tuple[str, date], Decimal | None] = {}

    async def rate(self, currency: str, day: date) -> Decimal | None:
        """Units of `currency` per EUR on `day` (or the last quote before it)."""
        currency = currency.upper()
        if currency == "EUR":
            return Decimal(1)
        key = (currency, day)
        if key not in self._cache:
            row = (
                await self.session.execute(
                    select(FxRate.rate_per_eur)
                    .where(
                        FxRate.currency == currency,
                        FxRate.day <= day,
                        FxRate.day > day - MAX_STALENESS,
                    )
                    .order_by(FxRate.day.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            self._cache[key] = row
        return self._cache[key]
