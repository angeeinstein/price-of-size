"""Tier 1: schema.org Product/Offer in JSON-LD (one parser for every shop)."""

import json
import math
import re
from collections.abc import Iterator
from typing import Any

from pos_shared import units
from pos_shared.extract.model import Extraction, MeasurementCandidate, OfferCandidate

AVAILABILITY = {
    "instock": "in_stock",
    "onlineonly": "in_stock",
    "instoreonly": "in_store_only",
    "limitedavailability": "in_stock",
    "outofstock": "out_of_stock",
    "soldout": "out_of_stock",
    "discontinued": "discontinued",
    "preorder": "preorder",
    "presale": "preorder",
    "backorder": "backorder",
    "madetoorder": "made_to_order",
}
CONDITION = {
    "newcondition": "new",
    "usedcondition": "used",
    "refurbishedcondition": "refurbished",
    "damagedcondition": "damaged",
}
PRODUCT_TYPES = {"product", "productgroup", "individualproduct", "productmodel", "vehicle", "car"}


def loads_lenient(text: str) -> Any | None:
    """JSON-LD in the wild: trailing commas, raw newlines in strings, HTML comments."""
    t = text.strip().removeprefix("<!--").removesuffix("-->").strip()
    for candidate in (t, re.sub(r",\s*([}\]])", r"\1", t.replace("\n", " ").replace("\r", " "))):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError, RecursionError):
            continue
    return None


def _types(node: Any) -> set[str]:
    if not isinstance(node, dict):
        return set()
    t = node.get("@type")
    if not isinstance(t, list):
        t = [t]
    return {x.rsplit("/", 1)[-1].lower() for x in t if isinstance(x, str) and x}


def walk(obj: Any, depth: int = 0) -> Iterator[dict]:
    """Every dict in a JSON-LD document, including @graph and nested entities."""
    if depth > 50:  # hostile nesting
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, depth + 1)


def top_level(obj: Any) -> Iterator[dict]:
    """The document's own entities: the root, a root list, or the @graph members."""
    items = obj if isinstance(obj, list) else [obj]
    for item in items:
        if isinstance(item, dict):
            yield item
            graph = item.get("@graph")
            if isinstance(graph, list):
                yield from (g for g in graph if isinstance(g, dict))


def _text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return _text(v.get("name") or v.get("@value") or v.get("value"))
    if isinstance(v, list):
        return _text(v[0]) if v else None
    s = str(v).strip()
    return s or None


def _enum(value: Any, table: dict[str, str]) -> str | None:
    s = _text(value)
    if not s:
        return None
    return table.get(s.rsplit("/", 1)[-1].lower().replace("_", ""))


def _price(v: Any) -> float | None:
    """A finite number (negative ones are kept, and flagged later), or None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v) if math.isfinite(v) else None
    text = str(v).strip()
    # schema.org asks for "." as decimal separator; shops do not always comply
    num = units.parse_number(text, "en" if "," not in text else "de")
    if num is None or not math.isfinite(num):
        return None
    return -num if text.startswith("-") else num


def _first_set(o: dict, *keys: str) -> Any:
    """The first value that is present (0 is a value, unlike `a or b`)."""
    for k in keys:
        if o.get(k) is not None:
            return o[k]
    return None


STRIKETHROUGH = {"strikethroughprice", "listprice", "msrp", "srp", "invoiceprice"}


def _offers(node: dict, currency_default: str | None = None) -> list[OfferCandidate]:
    out: list[OfferCandidate] = []
    raw = node.get("offers")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    for o in items:
        if not isinstance(o, dict):
            continue
        types = _types(o)
        availability = _enum(o.get("availability"), AVAILABILITY)
        condition = _enum(o.get("itemCondition") or node.get("itemCondition"), CONDITION)
        seller = _text(o.get("seller"))
        currency = _text(o.get("priceCurrency")) or currency_default
        if "aggregateoffer" in types:
            low_raw = _first_set(o, "lowPrice", "price")
            out.append(
                OfferCandidate(
                    _price(low_raw),
                    currency,
                    _text(low_raw),
                    availability,
                    condition,
                    seller,
                    "aggregate_low",
                    confidence=0.6,
                )
            )
            nested = o.get("offers")
            if nested:
                inner = {"offers": nested, "itemCondition": node.get("itemCondition")}
                out += _offers(inner, currency)
            continue
        price_raw = o.get("price")
        kind = "item"
        spec = o.get("priceSpecification")
        specs = spec if isinstance(spec, list) else [spec]
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            sp_types = _types(sp)
            price_type = (_text(sp.get("priceType")) or "").rsplit("/", 1)[-1].lower()
            if price_type in STRIKETHROUGH:
                continue  # the old/list price shown crossed out, not the price to pay
            is_unit = "unitpricespecification" in sp_types and sp.get("referenceQuantity")
            if price_raw is None and sp.get("price") is not None:
                price_raw = sp.get("price")
                currency = currency or _text(sp.get("priceCurrency"))
                if is_unit:
                    kind = "unit_price"  # only a base price (per kg, per l) was given
        out.append(
            OfferCandidate(
                _price(price_raw), currency, _text(price_raw), availability, condition, seller, kind
            )
        )
    return out


def _quantity(v: Any, kind: str, locale: str | None) -> tuple[float | None, str | None]:
    """QuantitativeValue {value, unitCode|unitText} or text like '12 kg' -> SI value.

    Text is read in the page's locale ('1,250 kg' on a German page is 1.25 kg).
    """
    if v is None:
        return None, None
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        value = v.get("value")
        code_text = _text(v.get("unitCode")) or ""
        code = code_text.upper()
        unit_text = (_text(v.get("unitText")) or "").lower()
        num = _price(value)
        if num is None or num <= 0:
            return None, None
        if code in units.UNIT_CODES and units.UNIT_CODES[code][0] == kind:
            return num * units.UNIT_CODES[code][1], code
        # unitText, or a plain unit ("cm", "kg") where a UN/CEFACT code belongs
        for unit in (unit_text, code_text.lower()):
            if unit:
                q = units.parse_quantity(f"{num} {unit}", kind, "en")
                if q:
                    return q.value, q.unit
        return None, None  # a number without unit is not guessed
    q = units.parse_quantity(str(v), kind, locale)
    return (q.value, q.unit) if q else (None, None)


def _measurement(node: dict, locale: str | None) -> MeasurementCandidate | None:
    raw: dict[str, Any] = {}
    axes = []
    for prop in ("width", "height", "depth", "length"):
        value, unit = _quantity(node.get(prop), "length", locale)
        if value is not None:
            axes.append(value)
            raw[prop] = node.get(prop)
    mass, _ = _quantity(node.get("weight"), "mass", locale)
    if mass is not None:
        raw["weight"] = node.get("weight")
    if not axes and mass is None:
        return None
    axes.sort(reverse=True)
    axes += [None] * (3 - len(axes))
    return MeasurementCandidate(
        basis="unknown",
        basis_method="parser_rule",
        a_m=axes[0],
        b_m=axes[1],
        c_m=axes[2],
        mass_kg=mass,
        raw=raw,
        method="jsonld",
        confidence=0.8,
    )


def _count(v: Any) -> int:
    if isinstance(v, list):
        return len(v)
    return 1 if isinstance(v, dict) else 0


def _additional_properties(node: dict) -> list[tuple[str, str]]:
    props = node.get("additionalProperty") or []
    props = props if isinstance(props, list) else [props]
    pairs = []
    for p in props:
        if isinstance(p, dict) and p.get("name") is not None and p.get("value") is not None:
            value = p.get("value")
            unit = _text(p.get("unitText")) or ""
            pairs.append((str(p["name"]), f"{value} {unit}".strip()))
    return pairs


def _position(item: dict) -> float:
    try:
        return float(item.get("position"))
    except (TypeError, ValueError):
        return math.inf  # unnumbered crumbs keep their order, after numbered ones


def _breadcrumbs(docs: list[Any]) -> list[str]:
    for doc in docs:
        for node in walk(doc):
            if "breadcrumblist" in _types(node):
                items = node.get("itemListElement") or []
                items = items if isinstance(items, list) else [items]
                items = sorted((i for i in items if isinstance(i, dict)), key=_position)
                names = [_text(i.get("name") or i.get("item")) for i in items]
                return [n for n in names if n]
    return []


def extract(
    json_ld_texts: list[str], ex: Extraction, locale: str | None = None
) -> list[tuple[str, str]]:
    """Fill `ex` from JSON-LD blocks. Returns label/value pairs (additionalProperty)
    for the spec-text tier."""
    docs = [d for d in (loads_lenient(t) for t in json_ld_texts) if d is not None]
    products = [n for d in docs for n in walk(d) if _types(n) & PRODUCT_TYPES]
    if not products:
        return []
    # The main product is the page's own entity (top level or @graph), preferably one
    # with offers; a nested product (a review's itemReviewed, an accessory) only if
    # there is none. Variants of a ProductGroup are counted (offers kept), not merged.
    tops = [n for d in docs for n in top_level(d) if _types(n) & PRODUCT_TYPES]
    variants = [
        v
        for t in tops
        for v in (t.get("hasVariant") if isinstance(t.get("hasVariant"), list) else [])
        if isinstance(v, dict) and v.get("offers")
    ]
    main = next(
        (p for p in [*tops, *variants] if p.get("offers")),
        tops[0] if tops else next((p for p in products if p.get("offers")), products[0]),
    )
    groups = [p for p in products if "productgroup" in _types(p)]
    if groups:
        ex.variants = sum(_count(g.get("hasVariant")) for g in groups)
    ex.sources.append("jsonld")
    ex.title = ex.title or _text(main.get("name"))
    ex.brand = ex.brand or _text(main.get("brand") or main.get("manufacturer"))
    for key in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8", "isbn"):
        digits = re.sub(r"\D", "", str(_text(main.get(key)) or ""))
        if digits:  # "n/a" in one key must not hide a code in the next
            ex.gtin = ex.gtin or digits
            break
    ex.mpn = ex.mpn or _text(main.get("mpn"))
    ex.sku = ex.sku or _text(main.get("sku") or main.get("productID"))
    category = _text(main.get("category"))
    ex.category_path = _breadcrumbs(docs) or (
        [c.strip() for c in re.split(r"[>/|]", category) if c.strip()] if category else []
    )
    ex.offers += _offers(main)
    m = _measurement(main, locale)
    if m:
        ex.measurements.append(m)
    return _additional_properties(main)
