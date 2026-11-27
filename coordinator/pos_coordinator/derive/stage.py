"""The extract stage: raw fetches -> products, offers, measurements.

Rebuildable by design (see docs/spec.md, "Hard-to-reverse decisions"): it reads
only the raw layer, every row it writes carries the versions that produced it,
and `rebuild()` drops the derived rows and runs it again from the start.

Progress is a cursor over raw_fetch (received_at, id), advanced in the same
transaction as the rows derived from it, so a crash neither duplicates nor
skips a fetch. Rows younger than SETTLE are left for the next round: a fetch
received earlier may still be committing, and a cursor must never pass it.
"""

import asyncio
import hashlib
import json
import logging
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any

import zstandard
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pos_coordinator.crawl import urls
from pos_coordinator.derive.fx import RateCache
from pos_coordinator.models import (
    Classification,
    DeriveCursor,
    Embedding,
    GoldLabel,
    Measurement,
    Offer,
    Product,
    RawFetch,
)
from pos_coordinator.rawstore import RawStore
from pos_shared.extract import EXTRACT_VERSION
from pos_shared.extract.model import Extraction, MeasurementCandidate
from pos_shared.extract.pipeline import extract_evidence
from pos_shared.extract.price_rules import price_flags
from pos_shared.units import UNITS_VERSION

log = logging.getLogger(__name__)

STAGE = "extract"
VALIDATOR_VERSION = "1"
VERSION = f"extract={EXTRACT_VERSION};units={UNITS_VERSION};validate={VALIDATOR_VERSION}"
SETTLE = timedelta(seconds=30)

# Offers with any of these flags do not count for a product's canonical price.
NOT_A_VALID_PRICE = {
    "placeholder_price",
    "unavailable",
    "unit_price",
    "aggregate_low_price",
    "no_price",
    "no_currency",
    "no_fx_rate",
    "monthly_price",
    "net_price",
    "from_price",
    "unit_price_text",
    "rental",
    "contract_price",
    "fetch_blocked",
    "fetch_throttled",
    "fetch_error",
    "fetch_gone",
}
OFFER_LEVEL = {
    "placeholder_price",
    "unavailable",
    "unit_price",
    "aggregate_low_price",
    "no_price",
    "no_currency",
}  # computed per offer, not per page

MIN_DENSITY, MAX_DENSITY = 1.0, 23_000.0  # kg/m³ (osmium is 22,600)
MAX_EXTENT_M = 1_000.0


@dataclass
class BatchReport:
    fetches: int = 0
    pages: int = 0
    offers: int = 0
    measurements: int = 0
    products: int = 0
    missing_blobs: int = 0


def gtin_valid(gtin: str) -> bool:
    """GS1 check digit (GTIN-8/12/13/14)."""
    if not gtin.isdigit() or len(gtin) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in gtin]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


def match_key(ex: Extraction, url: str) -> str:
    if ex.gtin and gtin_valid(ex.gtin.zfill(14) if len(ex.gtin) < 14 else ex.gtin):
        return f"gtin:{ex.gtin.zfill(14)}"
    if ex.brand and ex.mpn:
        return f"bm:{ex.brand.strip().lower()}|{ex.mpn.strip().lower()}"
    try:
        return f"url:{urls.normalize(url)}"
    except urls.BadUrl:
        return f"url:{url}"


def measurement_flags(m: MeasurementCandidate) -> list[str]:
    flags = []
    dims = [v for v in (m.a_m, m.b_m, m.c_m) if v is not None]
    if any(v <= 0 for v in dims) or (m.mass_kg is not None and m.mass_kg <= 0):
        flags.append("invalid_value")
    if dims and max(dims) > MAX_EXTENT_M:
        flags.append("implausible_size")
    if m.mass_kg and len(dims) == 3 and all(v > 0 for v in dims):
        density = m.mass_kg / (dims[0] * dims[1] * dims[2])
        if not MIN_DENSITY <= density <= MAX_DENSITY:
            flags.append("implausible_density")  # catches most mm/cm/m mix-ups
    return flags


def cross_checks(cands: list[MeasurementCandidate]) -> dict[int, list[str]]:
    """Gross mass >= net mass (package vs product), per the plausibility rules."""
    extra: dict[int, list[str]] = {}
    net = [
        m.mass_kg for m in cands if m.mass_kg and m.basis != "package" and m.net_or_gross != "gross"
    ]
    for i, m in enumerate(cands):
        is_gross = m.basis == "package" or m.net_or_gross == "gross"
        if m.mass_kg and is_gross and net and m.mass_kg < min(net) * 0.98:
            extra.setdefault(i, []).append("gross_lt_net")
    return extra


async def _cursor(session: AsyncSession) -> DeriveCursor:
    await session.execute(
        pg_insert(DeriveCursor)
        .values(stage=STAGE, version=VERSION)
        .on_conflict_do_nothing(index_elements=["stage"])
    )
    # FOR UPDATE: one derive runner at a time (a second coordinator slot just waits)
    return (
        await session.execute(
            select(DeriveCursor)
            .where(DeriveCursor.stage == STAGE)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _load(store: RawStore, sha: str) -> dict[str, Any] | None:
    if not store.exists("snippets", sha):
        return None
    return json.loads(zstandard.ZstdDecompressor().decompress(store.get("snippets", sha)))


async def process_batch(
    session: AsyncSession, store: RawStore, limit: int = 200, settle: timedelta = SETTLE
) -> BatchReport:
    """Derive up to `limit` raw fetches past the cursor; commits nothing itself."""
    report = BatchReport()
    cur = await _cursor(session)
    q = (
        select(RawFetch)
        .where(
            RawFetch.blob_sha256.is_not(None),
            RawFetch.mode.in_(("http", "browser")),
            RawFetch.received_at <= func.now() - settle,
        )
        .order_by(RawFetch.received_at, RawFetch.id)
        .limit(limit)
    )
    if cur.last_received_at is not None:
        q = q.where(
            or_(
                RawFetch.received_at > cur.last_received_at,
                and_(RawFetch.received_at == cur.last_received_at, RawFetch.id > cur.last_id),
            )
        )
    fetches = (await session.execute(q)).scalars().all()
    if not fetches:
        return report
    fx = RateCache(session)
    touched: set = set()
    for f in fetches:
        report.fetches += 1
        evidence = await asyncio.to_thread(_load, store, f.blob_sha256)
        if evidence is None:
            report.missing_blobs += 1
            continue
        if "snippets" not in evidence:
            continue  # robots.txt or sitemap evidence: nothing to derive
        ex = await asyncio.to_thread(extract_evidence, evidence)
        product_id = await _write(session, f, ex, fx, report)
        if product_id is not None:
            touched.add(product_id)
    last = fetches[-1]
    cur.last_received_at, cur.last_id = last.received_at, last.id
    cur.processed = (cur.processed or 0) + report.fetches
    cur.version = VERSION
    cur.updated_at = func.now()
    await _canonical_prices(session, touched)
    return report


async def _write(session, f: RawFetch, ex: Extraction, fx: RateCache, report) -> Any:
    if not (ex.offers or ex.measurements or ex.title):
        return None
    report.pages += 1
    url = f.final_url or f.url
    key = match_key(ex, url)
    stmt = pg_insert(Product).values(
        match_key=key,
        gtin=ex.gtin,
        brand=ex.brand,
        model=ex.mpn,
        title=ex.title,
        source=f.source,
        url=url,
        dimensionality="3d_object"
        if any(m.a_m for m in ex.measurements)
        else "bulk_unit"
        if any(m.true_volume_m3 for m in ex.measurements)
        else None,
        attributes={"category_path": ex.category_path, "sku": ex.sku, "variants": ex.variants},
        match_method=key.split(":", 1)[0],
        match_confidence=1.0 if key.startswith("gtin") else 0.8,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["match_key"],
        set_={
            "title": func.coalesce(Product.title, stmt.excluded.title),
            "brand": func.coalesce(Product.brand, stmt.excluded.brand),
            "dimensionality": func.coalesce(Product.dimensionality, stmt.excluded.dimensionality),
        },
    ).returning(Product.id, text("(xmax = 0) AS inserted"))
    product_id, inserted = (await session.execute(stmt)).one()
    report.products += int(bool(inserted))

    page_flags = sorted(set(ex.flags) - OFFER_LEVEL)
    day = f.fetched_at.astimezone(UTC).date()
    first_offer_id = None
    for o in ex.offers:
        flags = set(price_flags(o)) | set(page_flags)
        price_eur = fx_rate = None
        if o.price is not None and o.currency:
            rate = await fx.rate(o.currency, day)
            if rate is None:
                flags.add("no_fx_rate")
            else:
                fx_rate = rate
                price_eur = (Decimal(str(o.price)) / rate).quantize(Decimal("0.0001"))
        offer = Offer(
            product_id=product_id,
            fetch_id=f.id,
            source=f.source or "unknown",
            url=url,
            observed_on=f.fetched_at,
            raw_price=o.price_raw,
            price=Decimal(str(o.price)) if o.price is not None else None,
            currency=o.currency,
            fx_rate=fx_rate,
            price_eur=price_eur,
            vat="net" if "net_price" in flags else "gross",
            availability=o.availability,
            condition=o.condition,
            pack_size=ex.pack_size,
            flags=sorted(flags),
            fields={
                "price": {"method": o.method, "confidence": o.confidence, "kind": o.price_kind}
            },
            parser_version=EXTRACT_VERSION,
            validator_version=VALIDATOR_VERSION,
        )
        session.add(offer)
        await session.flush()
        first_offer_id = first_offer_id or offer.id
        report.offers += 1
    extra = cross_checks(ex.measurements)
    for i, m in enumerate(ex.measurements):
        session.add(
            Measurement(
                product_id=product_id,
                offer_id=first_offer_id,
                fetch_id=f.id,
                basis=m.basis,
                basis_method=m.basis_method,
                basis_confidence=m.confidence,
                a_m=m.a_m,
                b_m=m.b_m,
                c_m=m.c_m,
                mass_kg=m.mass_kg,
                net_or_gross=m.net_or_gross,
                true_volume_m3=m.true_volume_m3,
                raw={k: str(v) for k, v in m.raw.items()},
                method=m.method,
                confidence=m.confidence,
                parser_version=f"{EXTRACT_VERSION}/{UNITS_VERSION}",
                flags=sorted(set(measurement_flags(m)) | set(extra.get(i, []))),
            )
        )
        report.measurements += 1
    return product_id


async def _canonical_prices(session: AsyncSession, product_ids: set) -> None:
    """Canonical product price: the median of its valid offers in EUR."""
    if not product_ids:
        return
    await session.flush()
    rows = (
        await session.execute(
            select(Offer.product_id, Offer.price_eur, Offer.flags).where(
                Offer.product_id.in_(product_ids), Offer.price_eur.is_not(None)
            )
        )
    ).all()
    prices: dict[Any, list[Decimal]] = {}
    for pid, price, flags in rows:
        if not NOT_A_VALID_PRICE & set(flags or []):
            prices.setdefault(pid, []).append(price)
    for pid in product_ids:
        valid = prices.get(pid)
        await session.execute(
            update(Product)
            .where(Product.id == pid)
            .values(canonical_price_eur=statistics.median(valid) if valid else None)
        )


async def run_until_caught_up(
    sm: async_sessionmaker[AsyncSession],
    store: RawStore,
    limit: int = 200,
    settle: timedelta = SETTLE,
) -> BatchReport:
    total = BatchReport()
    while True:
        async with sm() as session:
            r = await process_batch(session, store, limit, settle)
            await session.commit()
        for field in total.__dataclass_fields__:
            setattr(total, field, getattr(total, field) + getattr(r, field))
        if r.fetches < limit:
            return total


async def loop(sm: async_sessionmaker[AsyncSession], store: RawStore, interval_s: float) -> None:
    while True:
        try:
            r = await run_until_caught_up(sm, store)
            if r.fetches:
                log.info("derive: %s", r)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("derive round failed")
        await asyncio.sleep(interval_s)


DERIVED_TABLES = (Measurement, Classification, Embedding, Offer, Product)


async def snapshot(session: AsyncSession) -> dict[str, Any]:
    out = {}
    for model in DERIVED_TABLES:
        out[model.__tablename__] = (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()
    digest = hashlib.sha256()
    for row in (
        await session.execute(
            select(Product.match_key, Product.canonical_price_eur).order_by(Product.match_key)
        )
    ).all():
        digest.update(repr(tuple(row)).encode())
    out["products_digest"] = digest.hexdigest()[:16]
    return out


async def rebuild(
    sm: async_sessionmaker[AsyncSession], store: RawStore, settle: timedelta = SETTLE
) -> dict[str, Any]:
    """The rebuild drill: drop the derived rows, derive everything again from raw, and
    report before/after. Gold labels (hand-checked) are kept: their links to derived
    rows are cleared, their link to the raw fetch stays."""
    started = time.monotonic()
    async with sm() as session:
        before = await snapshot(session)
        await session.execute(update(GoldLabel).values(product_id=None, offer_id=None))
        for model in DERIVED_TABLES:
            await session.execute(delete(model))
        await session.execute(delete(DeriveCursor).where(DeriveCursor.stage == STAGE))
        await session.commit()
    report = await run_until_caught_up(sm, store, settle=settle)
    async with sm() as session:
        after = await snapshot(session)
    return {
        "before": before,
        "after": after,
        "seconds": round(time.monotonic() - started, 2),
        "fetches": report.fetches,
        "version": VERSION,
        "identical": before == after,
    }
