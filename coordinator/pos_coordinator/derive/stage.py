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
from sqlalchemy import and_, case, delete, func, or_, select, text, update
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
    PriceReview,
    Product,
    RawFetch,
)
from pos_coordinator.rawstore import RawStore
from pos_shared.extract import EXTRACT_VERSION
from pos_shared.extract.model import Extraction, MeasurementCandidate
from pos_shared.extract.pipeline import api_records, extract_evidence
from pos_shared.extract.price_rules import price_flags
from pos_shared.units import UNITS_VERSION

log = logging.getLogger(__name__)

STAGE = "extract"
VALIDATOR_VERSION = "2"
VERSION = f"extract={EXTRACT_VERSION};units={UNITS_VERSION};validate={VALIDATOR_VERSION}"
# received_at is the start of the inserting transaction; a result transaction that
# commits later becomes visible behind it. Result transactions take seconds (lock
# timeouts of 10 s, retries), so ten minutes leaves a wide margin.
SETTLE = timedelta(minutes=10)
MAX_PRICE = Decimal("1e12")  # beyond any real offer (and below Numeric(18,4))

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
    "older_cost",
    "undated_cost",
    "review_rejected",  # the LLM review found it is not a valid price (validity/review.py)
}
OFFER_LEVEL = {
    "placeholder_price",
    "unavailable",
    "unit_price",
    "aggregate_low_price",
    "no_price",
    "no_currency",
    "older_cost",
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
    errors: int = 0  # fetches whose derivation raised: logged, counted, skipped
    store_unavailable: int = 0  # rounds that stopped because the raw store is not there


def gtin_valid(gtin: str) -> bool:
    """GS1 check digit (GTIN-8/12/13/14)."""
    if not gtin.isdigit() or len(gtin) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in gtin]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


def gtin_usable(gtin: str | None) -> bool:
    """A real GTIN: right length, valid check digit, not a filler such as 0000000000000.
    Short internal ids ("17") must not become GTINs by padding."""
    if not gtin or not gtin_valid(gtin):
        return False
    body = gtin.lstrip("0")
    return len(body) >= 7 and len(set(body[:-1])) > 1  # not all zeros / one repeated digit


def match_key(ex: Extraction, url: str) -> str:
    if gtin_usable(ex.gtin):
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
            RawFetch.mode.in_(("http", "browser", "api")),
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
    if not store.available():
        # An unmounted NAS looks like missing blobs: stop here, never skip past them.
        report.store_unavailable = 1
        return report
    fx = RateCache(session)
    touched: set = set()
    for f in fetches:
        report.fetches += 1
        try:
            # One fetch, one savepoint: a page that breaks a parser (or a value the
            # database refuses) is logged and skipped, it never stops the stage.
            async with session.begin_nested():
                product_ids = await _derive_one(session, store, f, fx, report)
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            cur.errors = (cur.errors or 0) + 1
            cur.last_error = f"{f.id}: {type(e).__name__}: {e}"[:2000]
            log.warning("derive: fetch %s failed: %s", f.id, cur.last_error, exc_info=True)
            continue
        touched |= product_ids
    if not store.available():
        await session.rollback()  # the store went away mid-batch: retry it all later
        return BatchReport(store_unavailable=1)
    last = fetches[-1]
    cur.last_received_at, cur.last_id = last.received_at, last.id
    cur.processed = (cur.processed or 0) + report.fetches
    cur.version = VERSION
    cur.updated_at = func.now()
    await _canonical_prices(session, touched)
    return report


async def _derive_one(session, store: RawStore, f: RawFetch, fx: RateCache, report) -> set:
    """The products a fetch touched: one for a shop page, one per item for an API
    response (a SPARQL result lists many)."""
    evidence = await asyncio.to_thread(_load, store, f.blob_sha256)
    if evidence is None:
        report.missing_blobs += 1
        return set()
    return await derive_evidence(session, store, f, evidence, fx, report)


async def derive_evidence(
    session, store: RawStore, f: RawFetch, evidence: Any, fx: RateCache, report
) -> set:
    """_derive_one with the fetch's evidence already loaded."""
    if not isinstance(evidence, dict):
        return set()
    if isinstance(evidence.get("api"), dict):
        touched = set()
        for rec in await asyncio.to_thread(api_records, evidence):
            pid = await _write(session, f, rec.extraction, fx, report, url=rec.url)
            if pid is not None:
                touched.add(pid)
        return touched
    if "snippets" not in evidence:
        return set()  # robots.txt or sitemap evidence: nothing to derive
    ex = await asyncio.to_thread(extract_evidence, evidence)
    if f.mode in ("http", "browser"):
        from pos_coordinator.derive import llm_tier, site_parsers

        lang = ((evidence.get("snippets") or {}).get("lang") or "")[:2].lower() or None
        await site_parsers.merge(session, store, f, ex, lang)  # tier 2: the site's parser
        await llm_tier.merge(session, f, ex, lang)  # tier 4: an answer, or a request for one
    pid = await _write(session, f, ex, fx, report)
    return {pid} if pid is not None else set()


def _provenance(f: RawFetch) -> str:
    """A fetch's place in the derive order, as a string that sorts like it."""
    at = f.received_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return f"{at}|{f.id}"


def _as_of(value: str | None):
    """'1999-01-04' -> date; None for anything else."""
    from datetime import date

    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _money(value: float | None) -> Decimal | None:
    """A price the derived layer can hold, or None (flagged by the caller)."""
    if value is None:
        return None
    d = Decimal(str(value))
    return d if d.is_finite() and abs(d) < MAX_PRICE else None


async def _write(
    session, f: RawFetch, ex: Extraction, fx: RateCache, report, url: str | None = None
) -> Any:
    """`url`: the record's own page when a fetch holds several (an API response)."""
    if not (ex.offers or ex.measurements or ex.title):
        return None
    report.pages += 1
    url = url or f.final_url or f.url
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
        attributes={
            "category_path": ex.category_path,
            "sku": ex.sku,
            "variants": ex.variants,
            # which fetch the breadcrumbs came from: (received_at, id) sorts in the
            # order the derive stage reads fetches
            "category_from": _provenance(f) if ex.category_path else None,
        },
        match_method=key.split(":", 1)[0],
        match_confidence=1.0 if key.startswith("gtin") else 0.8,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["match_key"],
        set_={
            "title": func.coalesce(Product.title, stmt.excluded.title),
            "brand": func.coalesce(Product.brand, stmt.excluded.brand),
            "dimensionality": func.coalesce(Product.dimensionality, stmt.excluded.dimensionality),
            # The product's shop category is the breadcrumbs of the earliest fetch, of
            # the product's own shop, that has any: the same whatever order fetches
            # and LLM answers arrive in (a page derived again keeps its place).
            "attributes": case(
                (
                    and_(
                        Product.source.is_not_distinct_from(stmt.excluded.source),
                        func.coalesce(
                            stmt.excluded.attributes["category_path"], text("'[]'::jsonb")
                        )
                        != text("'[]'::jsonb"),
                        or_(
                            func.coalesce(Product.attributes["category_path"], text("'[]'::jsonb"))
                            == text("'[]'::jsonb"),
                            func.coalesce(Product.attributes["category_from"].astext, "")
                            > stmt.excluded.attributes["category_from"].astext,
                        ),
                    ),
                    Product.attributes.op("||")(
                        func.jsonb_build_object(
                            "category_path",
                            stmt.excluded.attributes["category_path"],
                            "category_from",
                            stmt.excluded.attributes["category_from"],
                        )
                    ),
                ),
                else_=Product.attributes,
            ),
        },
    ).returning(Product.id, text("(xmax = 0) AS inserted"))
    product_id, inserted = (await session.execute(stmt)).one()
    report.products += int(bool(inserted))

    page_flags = sorted(set(ex.flags) - OFFER_LEVEL)
    day = f.fetched_at.astimezone(UTC).date()
    first_offer_id = None
    for o in ex.offers:
        flags = set(price_flags(o)) | set(page_flags)
        price = _money(o.price)
        if o.price is not None and price is None:
            flags.add("invalid_price")
        price_eur = fx_rate = None
        # a cost that refers to another date than the fetch's converts at that date
        fx_day = _as_of(o.as_of) or day
        if price is not None and o.currency:
            rate = await fx.rate(o.currency, fx_day)
            if rate is None:
                flags.add("no_fx_rate")
            else:
                fx_rate = rate
                price_eur = _money((price / rate).quantize(Decimal("0.0001")))
        offer = Offer(
            product_id=product_id,
            fetch_id=f.id,
            source=f.source or "unknown",
            url=url,
            observed_on=f.fetched_at,
            raw_price=o.price_raw,
            price=price,
            currency=o.currency,
            fx_rate=fx_rate,
            price_eur=price_eur,
            vat="net" if "net_price" in flags else "gross",
            availability=o.availability,
            condition=o.condition,
            pack_size=ex.pack_size,
            flags=sorted(flags),
            fields={
                "price": {
                    "method": o.method,
                    "confidence": o.confidence,
                    "kind": o.price_kind,
                    **({"as_of": o.as_of, "as_of_method": o.as_of_method} if o.as_of else {}),
                }
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


# Product-level price checks (docs/spec.md, "Detection pipeline", steps 2 and 4).
# They depend on every offer of a product, so they are recomputed whenever one
# arrives: the flags below are derived state, stripped and set again each time.
OUTLIER_FACTOR = Decimal(3)  # an offer this far from the product median is suspect
JUMP_FACTOR = Decimal(10)  # an order of magnitude between two visits of one page
CHECK_FLAGS = {"cross_source_outlier", "price_disagreement", "price_jump"}
NOT_A_VALID_PRICE |= {"cross_source_outlier", "price_disagreement"}


def _far(a: Decimal, b: Decimal, factor: Decimal) -> bool:
    return a > 0 and b > 0 and (a / b > factor or b / a > factor)


def price_checks(offers: list[tuple[Any, str, Any, Decimal | None, list[str]]]):
    """Offers of one product as (id, url, observed_on, price_eur, flags).

    Returns (new flags per offer id, canonical price). One page counts once: its
    latest observation (one fetch) stands for it, so a shop fetched daily does not
    outvote a shop fetched once. A fetch that found several offers (variants, a
    placeholder beside the real price) votes with the median of its valid ones, in
    whatever order the page listed them. The canonical price is the median of the
    pages whose vote passes every check.
    """
    flags: dict[Any, set[str]] = {o[0]: set(o[4] or []) - CHECK_FLAGS for o in offers}

    def valid(o) -> bool:
        return o[3] is not None and not (NOT_A_VALID_PRICE & flags[o[0]])

    # url -> observation time -> the offers of that fetch
    pages: dict[str, dict[Any, list]] = {}
    for o in offers:
        pages.setdefault(o[1] or str(o[0]), {}).setdefault(o[2], []).append(o)

    def vote(obs: list) -> Decimal | None:
        prices = [o[3] for o in obs if valid(o)]
        return statistics.median(prices) if prices else None

    latest: list[tuple[Decimal, list]] = []  # (page vote, the offers behind it)
    for observations in pages.values():
        history = [(when, obs, vote(obs)) for when, obs in sorted(observations.items())]
        voted = [(obs, v) for _, obs, v in history if v is not None]
        for (_, prev), (obs, cur) in zip(voted, voted[1:], strict=False):
            if _far(cur, prev, JUMP_FACTOR):
                for o in obs:
                    flags[o[0]].add("price_jump")
        _, last_obs, last_vote = history[-1]
        if last_vote is not None:
            latest.append((last_vote, [o for o in last_obs if valid(o)]))
    if len(latest) == 2 and _far(latest[0][0], latest[1][0], OUTLIER_FACTOR):
        for _, obs in latest:  # two pages that disagree: neither is believed
            for o in obs:
                flags[o[0]].add("price_disagreement")
        latest = []
    elif len(latest) >= 3:
        median = statistics.median(v for v, _ in latest)
        kept = []
        for v, obs in latest:
            if _far(v, median, OUTLIER_FACTOR):
                for o in obs:
                    flags[o[0]].add("cross_source_outlier")
            else:
                kept.append((v, obs))
        latest = kept
    votes = [v for v, _ in latest]
    return flags, (statistics.median(votes) if votes else None)


async def _canonical_prices(session: AsyncSession, product_ids: set) -> None:
    """Re-run the product-level price checks and set the canonical price."""
    if not product_ids:
        return
    await session.flush()
    rows = (
        await session.execute(
            select(
                Offer.product_id,
                Offer.id,
                Offer.url,
                Offer.observed_on,
                Offer.price_eur,
                Offer.flags,
            ).where(Offer.product_id.in_(product_ids))
        )
    ).all()
    by_product: dict[Any, list] = {}
    for pid, *offer in rows:
        by_product.setdefault(pid, []).append(tuple(offer))
    for pid in sorted(product_ids, key=str):
        offers = by_product.get(pid, [])
        new_flags, canonical = price_checks(offers)
        for oid, _url, _on, _price, old in offers:
            if set(old or []) != new_flags[oid]:
                await session.execute(
                    update(Offer).where(Offer.id == oid).values(flags=sorted(new_flags[oid]))
                )
        await session.execute(
            update(Product).where(Product.id == pid).values(canonical_price_eur=canonical)
        )


async def backfill_fx(sm: async_sessionmaker[AsyncSession], batch: int = 1000) -> int:
    """Offers stored while a rate was missing (ECB down, a new currency) get their EUR
    price once the rate is there; their products' price checks run again. Returns the
    number of offers converted. Offers still without a rate stay flagged."""
    total, after = 0, None
    while True:
        async with sm() as session:
            # The derive cursor's lock: never alongside a derive batch, which may be
            # adding offers to the same products (canonical prices from partial data).
            await _cursor(session)
            q = (
                select(Offer)
                .where(Offer.flags.contains(["no_fx_rate"]))
                .order_by(Offer.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
            if after is not None:
                q = q.where(Offer.id > after)
            offers = (await session.execute(q)).scalars().all()
            if not offers:
                return total
            after = offers[-1].id
            fx = RateCache(session)
            touched: set = set()
            for o in offers:
                if o.price is None or not o.currency:
                    continue
                # the date the price refers to, as at derivation (a historical cost)
                as_of = ((o.fields or {}).get("price") or {}).get("as_of")
                day = _as_of(as_of) or o.observed_on.astimezone(UTC).date()
                rate = await fx.rate(o.currency, day)
                if rate is None:
                    continue
                o.fx_rate = rate
                o.price_eur = _money((o.price / rate).quantize(Decimal("0.0001")))
                o.flags = sorted(set(o.flags or []) - {"no_fx_rate"})
                if o.product_id is not None:
                    touched.add(o.product_id)
                total += 1
            await _canonical_prices(session, touched)
            await session.commit()
            if len(offers) < batch:
                return total


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
            if r.store_unavailable:
                log.warning("derive: raw store unavailable (NAS not mounted?); waiting")
            elif r.fetches:
                log.info("derive: %s", r)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("derive round failed")
        await asyncio.sleep(interval_s)


DERIVED_TABLES = (Measurement, Classification, Embedding, PriceReview, Offer, Product)
# Offer flags set by stages that come after the rebuild (they need categories or
# LLM answers): left out of the comparison of canonical prices.
ASYNC_FLAGS = {"category_outlier", "review_rejected"}


async def snapshot(session: AsyncSession) -> dict[str, Any]:
    from pos_coordinator.classify import shopmap

    out = {}
    for model in DERIVED_TABLES:
        q = select(func.count()).select_from(model)
        if model is Classification:
            q = q.where(Classification.is_current)  # history rows are not rebuilt
        out[model.__tablename__] = (await session.execute(q)).scalar_one()
    # What the rebuild itself reproduces is the category each product's shop mapping
    # gives it, whichever tier holds the product now (an LLM pick, say, which comes
    # back only later): taken from the mappings, the same way on both sides.
    mapped = text(f"""
        SELECT p.id AS product_id, m.taxonomy_node_id
        FROM product p
        JOIN category_mapping m
          ON m.is_current AND m.source = p.source AND m.category_path = {shopmap.PATH_SQL}
        WHERE m.taxonomy_node_id IS NOT NULL
    """).columns(product_id=Product.id.type, taxonomy_node_id=Classification.taxonomy_node_id.type)
    mapped_sq = mapped.subquery()
    out["mapped_products"] = (
        await session.execute(select(func.count()).select_from(mapped_sq))
    ).scalar_one()
    # canonical prices as the price checks alone make them (the later stages' flags
    # left out): what the rebuild itself reproduces
    offers: dict[Any, list] = {}
    for pid, *o in (
        await session.execute(
            select(
                Offer.product_id,
                Offer.id,
                Offer.url,
                Offer.observed_on,
                Offer.price_eur,
                Offer.flags,
            )  # fmt: skip
        )
    ).all():
        o[4] = sorted(set(o[4] or []) - ASYNC_FLAGS)
        offers.setdefault(pid, []).append(tuple(o))
    digest = hashlib.sha256()
    for pid, key, node in (
        await session.execute(
            select(Product.id, Product.match_key, mapped_sq.c.taxonomy_node_id)
            .outerjoin(mapped_sq, mapped_sq.c.product_id == Product.id)
            .order_by(Product.match_key)
        )
    ).all():
        _, canonical = price_checks(offers.get(pid, []))
        digest.update(repr((key, canonical, node)).encode())
    out["products_digest"] = digest.hexdigest()[:16]
    return out


# Reported, not compared: computed later by workers (embeddings), by the k-NN and
# LLM tiers (which may hold products the rebuild first gives their shop mapping) and
# by the price review.
ASYNC_DERIVED = {"embedding", "classification", "price_review"}


def _comparable(snap: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in snap.items() if k not in ASYNC_DERIVED}


async def classify_until_settled(sm: async_sessionmaker[AsyncSession]) -> int:
    """Classification by shop mappings over all products (in batches); part of the
    rebuild, since categories belong to the derived layer while mappings survive it."""
    from pos_coordinator.classify import shopmap

    total = 0
    while True:
        async with sm() as session:
            r = await shopmap.classify(session)
            await session.commit()
        n = r["classified"] + r["withdrawn"]
        total += n
        if n == 0:
            return total


async def rebuild(
    sm: async_sessionmaker[AsyncSession], store: RawStore, settle: timedelta = SETTLE
) -> dict[str, Any]:
    """The rebuild drill: drop the derived rows, derive everything again from raw, and
    report before/after. Gold labels (hand-checked) are kept: their links to derived
    rows are cleared, their link to the raw fetch stays."""
    started = time.monotonic()
    async with sm() as session:
        cur = await _cursor(session)  # holds a running derive loop off until commit
        before = await snapshot(session)
        await session.execute(update(GoldLabel).values(product_id=None, offer_id=None))
        for model in DERIVED_TABLES:
            await session.execute(delete(model))
        cur.last_received_at = cur.last_id = cur.last_error = None
        cur.processed = cur.errors = 0
        cur.version = VERSION
        cur.started_at = cur.updated_at = func.now()
        await session.commit()
    report = await run_until_caught_up(sm, store, settle=settle)
    classified = await classify_until_settled(sm)
    async with sm() as session:
        after = await snapshot(session)
    return {
        "before": before,
        "after": after,
        "seconds": round(time.monotonic() - started, 2),
        "fetches": report.fetches,
        "classified": classified,
        "version": VERSION,
        # embeddings, the k-NN and LLM tiers and the price reviews come back later:
        # reported, not compared
        "identical": _comparable(before) == _comparable(after),
    }
