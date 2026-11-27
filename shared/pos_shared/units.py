"""Numbers, units, dimensions and masses from shop text, to SI.

Pure functions, no I/O: the same code runs during extraction and in tests, and
re-running it over stored raw evidence gives the same answer for the same
UNITS_VERSION. Bump UNITS_VERSION whenever a result can change.

Numbers: "1.299,00" / "1,299.00" / "1 299" / "12,5" / "12.5". With a single
separator followed by exactly three digits ("1.299", "1,299") the reading
depends on the locale (de: thousands separator "." / en: ","); callers pass the
page language as `locale`.
"""

import re
from dataclasses import dataclass, field

UNITS_VERSION = "2"

LENGTH_M = {
    "mm": 0.001,
    "millimeter": 0.001,
    "millimetre": 0.001,
    "cm": 0.01,
    "centimeter": 0.01,
    "centimetre": 0.01,
    "zentimeter": 0.01,
    "dm": 0.1,
    "m": 1.0,
    "meter": 1.0,
    "metre": 1.0,
    "km": 1000.0,
    "in": 0.0254,
    "inch": 0.0254,
    "inches": 0.0254,
    "zoll": 0.0254,
    '"': 0.0254,
    "″": 0.0254,
    "ft": 0.3048,
    "foot": 0.3048,
    "feet": 0.3048,
    "'": 0.3048,
    "′": 0.3048,
}
MASS_KG = {
    "mg": 1e-6,
    "g": 0.001,
    "gr": 0.001,
    "gramm": 0.001,
    "gram": 0.001,
    "grams": 0.001,
    "kg": 1.0,
    "kilo": 1.0,
    "kilogramm": 1.0,
    "kilogram": 1.0,
    "kilograms": 1.0,
    "t": 1000.0,
    "tonne": 1000.0,
    "tonnen": 1000.0,
    "tons": 1000.0,
    "lb": 0.45359237,
    "lbs": 0.45359237,
    "pound": 0.45359237,
    "pounds": 0.45359237,
    "oz": 0.028349523125,
    "ounce": 0.028349523125,
    "ounces": 0.028349523125,
}
VOLUME_M3 = {
    "ml": 1e-6,
    "cl": 1e-5,
    "dl": 1e-4,
    "l": 1e-3,
    "liter": 1e-3,
    "litre": 1e-3,
    "ltr": 1e-3,
    "m3": 1.0,
    "m³": 1.0,
    "cbm": 1.0,
    "cm3": 1e-6,
    "cm³": 1e-6,
    "ccm": 1e-6,
    "dm3": 1e-3,
    "dm³": 1e-3,
    "gal": 0.003785411784,
    "gallon": 0.003785411784,
}
# UN/CEFACT common codes used by schema.org QuantitativeValue.unitCode
UNIT_CODES = {
    "MMT": ("length", 0.001),
    "CMT": ("length", 0.01),
    "DMT": ("length", 0.1),
    "MTR": ("length", 1.0),
    "INH": ("length", 0.0254),
    "FOT": ("length", 0.3048),
    "GRM": ("mass", 0.001),
    "KGM": ("mass", 1.0),
    "TNE": ("mass", 1000.0),
    "LBR": ("mass", 0.45359237),
    "ONZ": ("mass", 0.028349523125),
    "MLT": ("volume", 1e-6),
    "LTR": ("volume", 1e-3),
    "MTQ": ("volume", 1.0),
    "CMQ": ("volume", 1e-6),
}

# Digits with "." / "," inside, or thousands grouped by (thin) spaces or apostrophes:
# "1 299,50", "1'299.50". A space only groups exactly three digits, so lists such
# as "20, 25, 30 cm" stay separate numbers.
_NUM = r"\d{1,3}(?:[ \u00a0\u202f\u2009'’]\d{3})+(?:[.,]\d+)?(?![\d.,])|\d[\d.,]*\d|\d"


def parse_number(text: str, locale: str | None = None) -> float | None:
    """'1.299,50' -> 1299.5; '1,299.50' -> 1299.5; '12,5' -> 12.5; None if no number."""
    m = re.search(_NUM, text)
    if not m:
        return None
    s = re.sub(r"['’ \u00a0\u202f\u2009]", "", m.group(0))  # spaces, apostrophes
    commas, dots = s.count(","), s.count(".")
    if commas and dots:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif commas or dots:
        sep = "," if commas else "."
        n = commas or dots
        tail = s.rsplit(sep, 1)[1]
        if n > 1:
            s = s.replace(sep, "")  # several: thousands separators
        elif len(tail) == 3 and len(s.split(sep)[0]) <= 3 and s.split(sep)[0] != "0":
            # ("0.250" is a decimal in every locale: no thousands group starts with 0)
            # "1.299" / "1,299": ambiguous; the locale decides which one groups thousands
            thousands = "." if (locale or "").startswith("de") else ","
            s = s.replace(sep, "") if sep == thousands else s.replace(sep, ".")
        else:
            s = s.replace(sep, ".")
    try:
        return float(s)
    except ValueError:
        return None


@dataclass(frozen=True)
class Quantity:
    kind: str  # length | mass | volume
    value: float  # SI: m, kg, m³
    raw: str
    unit: str


def _unit_table(kind: str) -> dict[str, float]:
    return {"length": LENGTH_M, "mass": MASS_KG, "volume": VOLUME_M3}[kind]


def _unit_pattern(kind: str) -> str:
    units = sorted(_unit_table(kind), key=len, reverse=True)
    return "|".join(re.escape(u) for u in units)


def parse_quantity(text: str, kind: str, locale: str | None = None) -> Quantity | None:
    """First '<number> <unit>' of the given kind, e.g. '12,5 kg' -> 12.5 kg."""
    pattern = rf"({_NUM})\s*({_unit_pattern(kind)})(?![a-zäöüß])"
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    value = parse_number(m.group(1), locale)
    if value is None:
        return None
    unit = m.group(2).lower()
    return Quantity(kind, value * _unit_table(kind)[unit], m.group(0), unit)


@dataclass(frozen=True)
class Dimensions:
    a: float  # m, sorted a >= b >= c
    b: float | None
    c: float | None
    raw: str
    unit: str
    labels: tuple[str, ...] = field(default=())  # e.g. ("B", "H", "T") in the source order

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(v for v in (self.a, self.b, self.c) if v is not None)


_SEP = r"\s*[x×*X]\s*"
_L = r"([A-ZÄÖÜa-zäöü])"
# "B×H×T:", "(B x H x T):", "L/B/H =" : the axis letters in source order
_LABELS = re.compile(rf"\(?\s*\b{_L}\s*[x×*X/]\s*{_L}(?:\s*[x×*X/]\s*{_L})?\b\s*\)?\s*[:=]")


def parse_dimensions(text: str, locale: str | None = None) -> Dimensions | None:
    """'120 x 80 x 45 cm', '120×80×45cm', '1,2 m x 80 cm', 'B×H×T: 120×80×45 cm'."""
    units = _unit_pattern("length")
    num_unit = rf"({_NUM})\s*({units})?"
    m = re.search(rf"{num_unit}{_SEP}{num_unit}(?:{_SEP}{num_unit})?(?![a-zäöüß])", text, re.I)
    if not m:
        return None
    nums = [m.group(1), m.group(3), m.group(5)]
    unit_groups = [m.group(2), m.group(4), m.group(6)]
    trailing = next((u for u in reversed(unit_groups) if u), None)
    if trailing is None:
        return None  # dimensions without any unit are too risky to guess
    values = []
    for n, u in zip(nums, unit_groups, strict=True):
        if n is None:
            continue
        v = parse_number(n, locale)
        if v is None:
            return None
        values.append(v * LENGTH_M[(u or trailing).lower()])
    values.sort(reverse=True)
    labels = ()
    lm = _LABELS.search(text)
    if lm:
        labels = tuple(g.upper() for g in lm.groups() if g)
    padded = values + [None] * (3 - len(values))
    return Dimensions(padded[0], padded[1], padded[2], m.group(0), trailing.lower(), labels)


# --- what a value describes ------------------------------------------------------------

PACKAGE_WORDS = re.compile(
    r"verpack|paket|karton|versand|lieferung|package|packag|shipping|carton|parcel|box\b", re.I
)
PRODUCT_WORDS = re.compile(
    r"\bprodukt|\bartikel|aufgebaut|montiert|assembled|\bproduct\b|\bitem\b|\bnetto\b", re.I
)
NET_WORDS = re.compile(r"netto|\bnet\b|eigengewicht|leergewicht", re.I)
GROSS_WORDS = re.compile(r"brutto|gross|gesamtgewicht|versandgewicht|shipping weight", re.I)


# "Gewicht ohne Verpackung", "Maße (unverpackt)", "weight without packaging"
NOT_PACKAGED = re.compile(
    r"unverpackt|\b(?:ohne|without|excl\.?|exkl\.?|excluding|not including)\s+"
    r"(?:\w+\s+)?(?:verpack|packag|karton|carton|box)",
    re.I,
)


def basis_of(label: str) -> str:
    """'product' | 'package' | 'unknown' from a label such as 'Paketmaße'."""
    if NOT_PACKAGED.search(label):
        return "product"
    if PACKAGE_WORDS.search(label):
        return "package"
    if PRODUCT_WORDS.search(label):
        return "product"
    return "unknown"


def net_or_gross(label: str) -> str | None:
    if GROSS_WORDS.search(label):
        return "gross"
    if NET_WORDS.search(label):
        return "net"
    return None


# --- single axes from labelled values ----------------------------------------------------

AXIS_WORDS = {
    "width": re.compile(r"\bbreite\b|\bwidth\b|\bbreit\b|^b$", re.I),
    "height": re.compile(r"\bhöhe\b|\bhoehe\b|\bheight\b|\bhoch\b|^h$", re.I),
    "depth": re.compile(r"\btiefe\b|\bdepth\b|\btief\b|^t$|^d$", re.I),
    "length": re.compile(r"\blänge\b|\blaenge\b|\blength\b|\blang\b|^l$", re.I),
    "diameter": re.compile(r"durchmesser|diameter|ø|⌀", re.I),
    "thickness": re.compile(r"\bstärke\b|\bdicke\b|thickness", re.I),
}


def axis_of(label: str) -> str | None:
    for axis, pattern in AXIS_WORDS.items():
        if pattern.search(label.strip()):
            return axis
    return None
