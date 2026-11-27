"""Tier 1: schema.org Product/Offer in JSON-LD (one parser for every shop)."""

import json
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
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    if isinstance(t, str):
        t = [t]
    return {str(x).rsplit("/", 1)[-1].lower() for x in t if x}


def walk(obj: Any) -> Iterator[dict]:
    """Every dict in a JSON-LD document, including @graph and nested entities."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


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
    if v is None:
        return None
    if isinstance(v, int | float):
        return float(v)
    # schema.org asks for "." as decimal separator; shops do not always comply
    return units.parse_number(str(v), "en" if "," not in str(v) else "de")


def _offers(node: dict) -> list[OfferCandidate]:
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
        currency = _text(o.get("priceCurrency"))
        if "aggregateoffer" in types:
            low = _price(o.get("lowPrice") or o.get("price"))
            out.append(
                OfferCandidate(
                    low,
                    currency,
                    _text(o.get("lowPrice") or o.get("price")),
                    availability,
                    condition,
                    seller,
                    "aggregate_low",
                    confidence=0.6,
                )
            )
            nested = o.get("offers")
            if nested:
                out += _offers({"offers": nested, "itemCondition": node.get("itemCondition")})
            continue
        price_raw = o.get("price")
        kind = "item"
        spec = o.get("priceSpecification")
        specs = spec if isinstance(spec, list) else [spec] if isinstance(spec, dict) else []
        for sp in specs:
            if price_raw is None and sp.get("price") is not None:
                price_raw = sp.get("price")
                currency = currency or _text(sp.get("priceCurrency"))
            if "unitpricespecification" in _types(sp) and sp.get("referenceQuantity"):
                kind = "unit_price"  # price per 100 g / per litre: not the item price
        out.append(
            OfferCandidate(
                _price(price_raw), currency, _text(price_raw), availability, condition, seller, kind
            )
        )
    return out


def _quantity(v: Any, kind: str) -> tuple[float | None, str | None]:
    """QuantitativeValue {value, unitCode|unitText} or text like '12 kg' -> SI value."""
    if v is None:
        return None, None
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        value = v.get("value")
        code = (_text(v.get("unitCode")) or "").upper()
        unit_text = (_text(v.get("unitText")) or "").lower()
        num = _price(value)
        if num is None:
            return None, None
        if code in units.UNIT_CODES and units.UNIT_CODES[code][0] == kind:
            return num * units.UNIT_CODES[code][1], code
        if unit_text:
            q = units.parse_quantity(f"{value} {unit_text}", kind, "en")
            return (q.value, q.unit) if q else (None, None)
        return None, None  # a number without unit is not guessed
    q = units.parse_quantity(str(v), kind, "en")
    return (q.value, q.unit) if q else (None, None)


def _measurement(node: dict) -> MeasurementCandidate | None:
    raw: dict[str, Any] = {}
    axes = []
    for prop in ("width", "height", "depth", "length"):
        value, unit = _quantity(node.get(prop), "length")
        if value is not None:
            axes.append(value)
            raw[prop] = node.get(prop)
    mass, _ = _quantity(node.get("weight"), "mass")
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


def _breadcrumbs(docs: list[Any]) -> list[str]:
    for doc in docs:
        for node in walk(doc):
            if "breadcrumblist" in _types(node):
                items = node.get("itemListElement") or []
                items = sorted(
                    (i for i in items if isinstance(i, dict)), key=lambda i: i.get("position", 0)
                )
                names = [_text(i.get("name") or i.get("item")) for i in items]
                return [n for n in names if n]
    return []


def extract(json_ld_texts: list[str], ex: Extraction) -> list[tuple[str, str]]:
    """Fill `ex` from JSON-LD blocks. Returns label/value pairs (additionalProperty)
    for the spec-text tier."""
    docs = [d for d in (loads_lenient(t) for t in json_ld_texts) if d is not None]
    products = [n for d in docs for n in walk(d) if _types(n) & PRODUCT_TYPES]
    if not products:
        return []
    # Prefer the main product: one with offers, else the first. Variants of a
    # ProductGroup are counted (and their offers kept) but not merged.
    main = next((p for p in products if p.get("offers")), products[0])
    groups = [p for p in products if "productgroup" in _types(p)]
    if groups:
        ex.variants = sum(len(g.get("hasVariant") or []) for g in groups)
    ex.sources.append("jsonld")
    ex.title = ex.title or _text(main.get("name"))
    ex.brand = ex.brand or _text(main.get("brand") or main.get("manufacturer"))
    for key in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8", "isbn"):
        if main.get(key):
            ex.gtin = ex.gtin or re.sub(r"\D", "", str(main[key])) or None
            break
    ex.mpn = ex.mpn or _text(main.get("mpn"))
    ex.sku = ex.sku or _text(main.get("sku") or main.get("productID"))
    category = _text(main.get("category"))
    ex.category_path = _breadcrumbs(docs) or (
        [c.strip() for c in re.split(r"[>/|]", category) if c.strip()] if category else []
    )
    ex.offers += _offers(main)
    m = _measurement(main)
    if m:
        ex.measurements.append(m)
    return _additional_properties(main)
