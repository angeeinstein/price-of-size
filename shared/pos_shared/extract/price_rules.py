"""Rule flags for prices (the spec's trap table). Flags only: nothing is dropped,
analysis decides what to filter."""

import html
import re

from lxml import html as lxml_html

from pos_shared.extract.model import Extraction, OfferCandidate

PLACEHOLDERS = {0.0, 0.01, 1.0}
# 99999, 999999.99, 9999999.00: "price on request" fillers. Not 999 or 9999.99,
# which are ordinary prices.
NINES = re.compile(r"^9{5,}\.(?:9+|0+)$")


def _rx(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(alternatives), re.I)


TEXT_FLAGS = [
    (
        "monthly_price",
        _rx(r"/\s*monat", "pro monat", r"\bmtl\.", "monatlich", "per month", r"/\s*mo\b"),
    ),
    (
        "net_price",
        _rx(
            r"zzgl\.?\s*mwst", r"exkl\.?\s*mwst", r"excl\.?\s*vat", "ohne mwst", r"netto(preis)?\b"
        ),
    ),
    ("from_price", _rx(r"(^|\s)(ab|from|starting at|bereits ab)\s*(€|eur|\$|\d)")),
    (
        "unit_price_text",
        _rx(r"(pro|je|per)\s*(100\s*)?(kg|g|l|ml|m|m²|stück)\b", r"/\s*(kg|l|m²|100\s*g)\b"),
    ),
    ("rental", _rx(r"\bmiete\b", "mieten", "leasing", "rental", r"\brent\b")),
    ("contract_price", _rx("mit vertrag", "vertragslaufzeit", "with contract", "with plan")),
]

PACK = [
    re.compile(r"(\d{1,3})\s*er[\s-]*(set|pack|packung|karton)", re.I),
    re.compile(r"(?:pack|set|box|case)\s+of\s+(\d{1,3})", re.I),
    # "6 x 0,5 l", "4 x 125 g": a count times a quantity. Not "100x60x201 cm" (dimensions).
    re.compile(
        r"(\d{1,3})\s*[x×]\s*\d+(?:[.,]\d+)?\s*(?:ml|cl|l|g|kg|stück|stk)\b(?!\s*[x×])", re.I
    ),
    re.compile(r"(\d{1,3})\s*(?:stück|stk\.?|pcs|pieces|count)\b", re.I),
]


# availability that keeps an offer's price out of the product's price
UNAVAILABLE = ("out_of_stock", "discontinued")


def price_flags(offer: OfferCandidate) -> list[str]:
    flags = []
    p = offer.price
    if p is None:
        flags.append("no_price")
    elif p < 0 or p in PLACEHOLDERS or NINES.match(f"{p:.2f}"):
        flags.append("placeholder_price")
    if offer.availability in UNAVAILABLE:
        flags.append("unavailable")
    if offer.price_kind == "unit_price":
        flags.append("unit_price")
    if offer.price_kind == "aggregate_low":
        flags.append("aggregate_low_price")
    if offer.price_kind == "older_cost":
        flags.append("older_cost")  # a newer cost of the same item exists
    if offer.condition and offer.condition != "new":
        flags.append(f"condition_{offer.condition}")
    if not offer.currency:
        flags.append("no_currency")
    return flags


def visible_text(fragment: str) -> str:
    """The text a reader sees in a captured HTML block ("ab <b>199 €</b>" -> "ab 199 €"),
    whitespace collapsed. Plain text passes through unchanged."""
    if "<" not in fragment:
        return " ".join(html.unescape(fragment).split())
    try:
        root = lxml_html.fragment_fromstring(fragment, create_parent="div")
        for el in root.iter():  # element boundaries separate words
            el.tail = " " + (el.tail or "")
        text = root.text_content()
    except Exception:  # noqa: BLE001 - broken markup: drop the tags by hand
        text = html.unescape(re.sub(r"<[^>]*>", " ", fragment))
    return " ".join(text.split())


def text_flags(price_blocks: list[str], title: str | None) -> list[str]:
    text = " ".join(visible_text(b) for b in price_blocks[:20]) + " " + (title or "")
    return [name for name, pattern in TEXT_FLAGS if pattern.search(text)]


def pack_size(title: str | None) -> float | None:
    if not title:
        return None
    for pattern in PACK:
        m = pattern.search(title)
        if m:
            n = int(m.group(1))
            if 2 <= n <= 500:
                return float(n)
    return None


def apply(ex: Extraction, price_blocks: list[str]) -> None:
    for o in ex.offers:
        for f in price_flags(o):
            if f not in ex.flags:
                ex.flags.append(f)
    for f in text_flags(price_blocks, ex.title):
        if f not in ex.flags:
            ex.flags.append(f)
    ex.pack_size = pack_size(ex.title)
    if ex.pack_size:
        ex.flags.append("pack")
