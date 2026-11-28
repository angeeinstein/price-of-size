"""Tier 4 of extraction: what the LLM sees of a page, and how its answer becomes
candidates (docs/spec.md, "Extraction", tier 4).

The LLM reads a condensed text of the captured snippets (title, description,
breadcrumbs, price blocks, spec tables). It does not compute anything: it copies
the price as written and the size and weight lines with their labels, and the
same parsers as for every other tier turn those into numbers. So a number format
or unit rule fixed in code applies to LLM answers too, and an LLM answer is
evidence with a label, not a guessed value.

An answer only fills what the other tiers did not find: a price when no offer had
one, sizes when no measurement had any. Everything it adds has method "llm".
"""

from typing import Any

from pos_shared import units
from pos_shared.extract import spectext
from pos_shared.extract.model import Extraction, MeasurementCandidate, OfferCandidate
from pos_shared.extract.price_rules import visible_text

PROMPT_VERSION = "llm-extract-2"  # 2: room for thinking (1 could cut answers off)
MAX_CHARS = 7_000
PART_CHARS = {"title": 300, "description": 500, "breadcrumbs": 300, "prices": 1_200}
CONFIDENCE = 0.6
PRICE_KINDS = ("item", "unit_price", "from_price", "monthly", "net", "unknown")
# price kinds the answer can name, as offer kinds and page flags of the other tiers
KIND_TO_OFFER = {"unit_price": "unit_price", "from_price": "from_price"}
KIND_TO_FLAG = {"monthly": "monthly_price", "net": "net_price", "from_price": "from_price"}


def _texts(fragments: list[Any], limit: int, each: int = 300) -> list[str]:
    out: list[str] = []
    used = 0
    for f in fragments or []:
        t = visible_text(str(f))[:each]
        if not t or t in out:
            continue
        if used + len(t) > limit:
            break
        out.append(t)
        used += len(t)
    return out


def page_text(evidence: dict[str, Any]) -> str:
    """The page as the LLM reads it: deterministic, at most MAX_CHARS."""
    snip = evidence.get("snippets") or {}
    meta = snip.get("meta") or {}
    parts = []
    title = " ".join(str(snip.get("title") or "").split())[: PART_CHARS["title"]]
    if title:
        parts.append(f"Title: {title}")
    for key in ("og:description", "description"):
        vals = meta.get(key) if isinstance(meta, dict) else None
        if vals:
            text = " ".join(str(vals[0]).split())[: PART_CHARS["description"]]
            if text:
                parts.append(f"Description: {text}")
                break
    crumbs = _texts(snip.get("breadcrumbs") or [], PART_CHARS["breadcrumbs"])
    if crumbs:
        parts.append(f"Breadcrumbs: {crumbs[0]}")
    prices = _texts(snip.get("price_blocks") or [], PART_CHARS["prices"], each=200)
    if prices:
        parts.append("Price texts:\n" + "\n".join(f"- {p}" for p in prices))
    head = "\n".join(parts)
    room = MAX_CHARS - len(head) - 20
    specs = _texts(
        (snip.get("tables") or [])
        + (snip.get("definition_lists") or [])
        + (snip.get("spec_blocks") or []),
        max(room, 0),
        each=2_000,
    )
    if specs:
        head += "\nDetails:\n" + "\n".join(f"- {s}" for s in specs)
    return head[:MAX_CHARS]


def missing(ex: Extraction) -> list[str]:
    """What the other tiers did not find on a page that looks like a product's."""
    return gaps(ex) if ex.title else []


def gaps(ex: Extraction) -> list[str]:
    """What an extraction lacks: a price (with its currency) and sizes, whatever its
    title."""
    out = []
    if not any(o.price is not None and o.currency for o in ex.offers):
        out.append("price")
    if not any(m.has_size for m in ex.measurements):
        out.append("size")
    return out


def _clean(v: Any, n: int = 300) -> str:
    return " ".join(str(v or "").split())[:n]


def apply(ex: Extraction, answer: dict[str, Any], locale: str | None) -> bool:
    """Fill the gaps of `ex` from a valid answer (see the schema in the coordinator's
    derive/llm_tier.py). Returns whether anything was added."""
    if not answer.get("is_product_page"):
        return False
    need = missing(ex)
    added = False
    if "price" in need:
        raw = _clean(answer.get("price_text"), 100)
        price = units.parse_number(raw, locale) if raw else None
        currency = _clean(answer.get("currency"), 3).upper() or None
        if price is not None:
            kind = answer.get("price_kind") if answer.get("price_kind") in PRICE_KINDS else None
            ex.offers.append(
                OfferCandidate(
                    price=price,
                    currency=currency if currency and currency.isalpha() else None,
                    price_raw=raw,
                    availability=_enum(answer.get("availability")),
                    condition=_enum(answer.get("condition")),
                    price_kind=KIND_TO_OFFER.get(kind or "", "item"),
                    method="llm",
                    confidence=CONFIDENCE,
                )
            )
            flag = KIND_TO_FLAG.get(kind or "")
            if flag and flag not in ex.flags:
                ex.flags.append(flag)
            added = True
    if "size" in need:
        pairs = [
            (_clean(s.get("label"), 100), _clean(s.get("value"), 200))
            for s in answer.get("sizes") or []
            if isinstance(s, dict)
        ]
        found: list[MeasurementCandidate] = spectext.measurements(
            [p for p in pairs if p[0] and p[1]][:30], locale
        )
        for m in found:
            m.method, m.confidence = "llm", CONFIDENCE
        ex.measurements += found
        added = added or bool(found)
    if not ex.category_path:
        path = [_clean(c, 100) for c in answer.get("category_path") or [] if _clean(c, 100)]
        ex.category_path = path[:8]
    # (no brand, GTIN or MPN from an answer: they decide the product's match key, and a
    # product must keep the identity its first derivation gave it)
    if added:
        ex.sources.append("llm")
    return added


def _enum(v: Any) -> str | None:
    return v if isinstance(v, str) and v != "unknown" else None
