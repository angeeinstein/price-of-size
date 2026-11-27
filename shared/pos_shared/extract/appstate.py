"""Embedded application state (Next.js, Nuxt, hydration JSON) -> label/value pairs.

The worker keeps only the small objects of a page's state that describe a size,
with their path ("product.packageMeasurements[0]") and the product that owns them
(the nearest enclosing object with a sku, gtin, ...). Here they become the same
label/value pairs a spec table gives, so the spec-text tier decides what they mean:
the path says whether a node describes the package, the key which axis or mass.

State often holds more than the page's product (recommendations, accessories, a
product list). Only one product's nodes are used: the one whose identifiers match the
page's JSON-LD product, else the one shallowest owner outside related-product blocks;
if that is ambiguous, none.
"""

import re
from typing import Any

from pos_shared import units
from pos_shared.extract import spectext

_PACKAGE_WORDS = r"packages?|packaging|shipping|cartons?|parcels?|box(?:es)?|verpackung|pakete?"
_BOX_WORDS = r"packages?|packaging|cartons?|parcels?|box(?:es)?|verpackung|pakete?"


def _words(segment: str) -> list[str]:
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", segment)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9äöüÄÖÜß]+", split) if w]


def _named(segment: str, words: str) -> bool:
    """The segment's first or last word names it ("packageMeasurements", "shippingBox",
    "verpackungsmasse"); a word in the middle does not ("catalogBoxSlim")."""
    w = _words(segment)
    return bool(w) and bool(re.match(words, w[0]) or re.fullmatch(words, w[-1]))


def is_package_segment(segment: str) -> bool:
    return _named(segment, _PACKAGE_WORDS)


def is_box_list_segment(segment: str) -> bool:
    """A list whose entries are boxes ("packages", "packageMeasurements"), not shipping
    options or methods."""
    return _named(segment, _BOX_WORDS)


# Blocks of other products than the page's own.
RELATED_PATH = re.compile(
    r"recommend|related|similar|suggest|accessor|cross_?sell|upsell|also_?(bought|viewed)|"
    r"recently|bundle|complement|carousel|compare|teaser|sponsored|zubehoer|zubehör|aehnlich",
    re.I,
)
# A list of products: each index is another product ("products[3].package").
PRODUCT_LIST = re.compile(r"product|item|article|artikel|offer|variant|hit|result|tile|card", re.I)
# key (lowercase, letters only) -> label understood by the spec-text tier
LABELS = {
    "weight": "weight",
    "netweight": "net weight",
    "grossweight": "gross weight",
    "height": "height",
    "width": "width",
    "depth": "depth",
    "length": "length",
    "diameter": "diameter",
    "volume": "volume",
    "dimensions": "dimensions",
}


UNIT_IN_TEXT = re.compile(r"[^\d\s.,]+$")
_KNOWN_UNITS = {**units.LENGTH_M, **units.MASS_KG, **units.VOLUME_M3}
# UN/CEFACT codes (KGM, CMT, ...) -> a symbol the unit parser reads, same factor.
_CODE_SYMBOL = {
    code: next(
        (
            sym
            for sym, f in {
                "length": units.LENGTH_M,
                "mass": units.MASS_KG,
                "volume": units.VOLUME_M3,
            }[kind].items()
            if f == factor and sym.isascii()
        ),
        None,
    )
    for code, (kind, factor) in units.UNIT_CODES.items()
}


def _num(value: float, unit: str) -> str:
    """Four decimals: never read as a thousands group, in any locale ("1.2500 kg")."""
    return f"{value:.4f} {unit}"


def _key(k: str) -> str:
    return re.sub(r"[^a-z]", "", k.lower())


def _unit(unit: Any) -> str | None:
    """A unit the parser reads: a symbol ("kg", "cm") or a UN/CEFACT code ("KGM")."""
    if not isinstance(unit, str) or not unit.strip():
        return None
    u = unit.strip()
    if u.upper() in _CODE_SYMBOL:
        return _CODE_SYMBOL[u.upper()]
    return u if u.lower() in _KNOWN_UNITS else None


def _value_text(v: Any, node: dict) -> str | None:
    """'27.5600 kg' from {'text': '27.56 kg', 'value': 27.56}, {'value': 27.56, 'unit':
    'kg'}, {'value': 27.56, 'unitCode': 'KGM'} or a bare number with a sibling unit;
    '27,56 kg' from a plain string. A number without any unit is not guessed."""
    if isinstance(v, dict):
        text = v.get("text") or v.get("formatted") or v.get("display")
        value = v.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            # The number itself, with the unit from the text: state JSON writes "27.56 kg"
            # whatever the page's language, and "1.250 kg" read as German is 1250 kg.
            unit = _unit(v.get("unit") or v.get("unitOfMeasure") or v.get("unitCode"))
            if unit is None and isinstance(text, str):
                m = UNIT_IN_TEXT.search(text.strip())
                unit = _unit(m.group(0)) if m else None
            if unit is not None:
                return _num(value, unit)
        if isinstance(text, str) and text.strip():
            return text
        return None
    if isinstance(v, str):
        return v if v.strip() else None
    if isinstance(v, int | float) and not isinstance(v, bool):
        unit = _unit(node.get("unit") or node.get("unitOfMeasure") or node.get("unitCode"))
        return _num(v, unit) if unit else None
    return None


_TOKEN = re.compile(r"\[\d+\]|[^.\[\]]+")


def _join(tokens: list[str]) -> str:
    return "".join(t if t.startswith("[") or i == 0 else f".{t}" for i, t in enumerate(tokens))


def package_root(path: str) -> str | None:
    """The object a package node belongs to: through the segment naming the package
    and any list index after it. "p.packages[1].dimensions" and "p.packages[1]" are one
    box, "p.packageMeasurements[0]" and "p.packageMeasurements[1]" two. None: not a
    package."""
    tokens = _TOKEN.findall(path)
    named = [i for i, t in enumerate(tokens) if not t.startswith("[") and is_package_segment(t)]
    if not named:
        return None
    end = named[-1]
    indexes = [i for i in range(end + 1, len(tokens)) if tokens[i].startswith("[")]
    if indexes:
        end = indexes[-1]
    return _join(tokens[: end + 1])


def _owner(item: dict) -> str:
    """Path of the product a node belongs to: the worker's owner (an object with a sku,
    gtin, ...), else an entry of a product list above the package ("products[3]"), else
    the page ("")."""
    owner = item.get("owner")
    if isinstance(owner, dict) and isinstance(owner.get("path"), str):
        return owner["path"]
    tokens = _TOKEN.findall(item.get("path", ""))
    end = None
    for i, t in enumerate(tokens):
        if not t.startswith("[") and is_package_segment(t):
            break  # indexes below the package are its boxes, not products
        if t.startswith("[") and i and PRODUCT_LIST.search(tokens[i - 1]):
            end = i
    return _join(tokens[: end + 1]) if end is not None else ""


# Shop item numbers are the shop's SKU under another name.
_SKU_KEYS = {"sku", "itemno", "itemnumber", "articlenumber", "articleno", "artnr",
             "productnumber", "productid"}  # fmt: skip


def _kind(key: str) -> str:
    if key.startswith("gtin") or key == "ean":
        return "gtin"
    return "sku" if key in _SKU_KEYS else key


def _canon(key: str, value: str) -> str:
    """Comparable form: GTINs as digits without leading zeros, SKUs and MPNs without
    case and separators ("202.133.12" is item 20213312)."""
    v = value.strip().lower()
    if _kind(key) == "gtin":
        return re.sub(r"\D", "", v).lstrip("0")
    return re.sub(r"[\s._/-]", "", v)


def _matches(item: dict, main: dict[str, str | None]) -> bool:
    ids = (item.get("owner") or {}).get("ids") or {}
    if not isinstance(ids, dict):
        return False
    wanted: dict[str, set[str]] = {}
    for k, v in main.items():
        if isinstance(v, str) and _canon(k, v):
            # a JSON-LD mpn is often the shop's item number too
            for kind in {_kind(k), "sku"} if k == "mpn" else {_kind(k)}:
                wanted.setdefault(kind, set()).add(_canon(k, v))
    return any(
        isinstance(v, str) and _canon(k, v) in wanted.get(_kind(k), set()) for k, v in ids.items()
    )


def _related(path: str) -> bool:
    return any(RELATED_PATH.search(t) for t in _TOKEN.findall(path) if not t.startswith("["))


def _owner_key(item: dict, owner_path: str) -> str:
    """One product described twice (IKEA: in the page state and in a component's
    props) has one key: its identifiers. Without any, its path."""
    ids = (item.get("owner") or {}).get("ids") or {}
    strong = {k: v for k, v in ids.items() if k not in ("name", "title", "id")} or ids
    if isinstance(strong, dict) and strong:
        return "ids:" + "|".join(f"{k}={_canon(k, str(v))}" for k, v in sorted(strong.items()))
    return owner_path


def main_product(state: list[dict], main: dict[str, str | None] | None = None) -> list[dict]:
    """The nodes of the page's own product (see the module docstring)."""
    items = []
    for it in state:
        if not isinstance(it, dict) or not isinstance(it.get("node"), dict):
            continue
        owner = _owner(it)
        # A related block below the owner holds other products (also ones without
        # identifiers, which would otherwise count as the page's own).
        if _related(it.get("path", "")[len(owner) :]):
            continue
        items.append((it, owner, bool(_measures(it["node"]))))
    # Boxes are known from lists that measure at least one of them; a list that measures
    # none only describes them ("1 package, item 202.133.12") and is left out. In a list
    # that does, a box without measurements stays: it stops the package total.
    measured_lists = {_box_list(it.get("path", "")) for it, _, m in items if m} - {None}
    groups: dict[str, list[dict]] = {}
    paths: dict[str, list[str]] = {}
    for it, owner, measured in items:
        if measured or _box_list(it.get("path", "")) in measured_lists:
            key = _owner_key(it, owner)
            groups.setdefault(key, []).append(it)
            paths.setdefault(key, []).append(owner)
    groups = {k: its for k, its in groups.items() if any(_measures(i["node"]) for i in its)}
    matched = [k for k, its in groups.items() if main and any(_matches(it, main) for it in its)]
    if len(matched) == 1:
        return _one_representation(groups[matched[0]], paths[matched[0]])

    def depth(key: str) -> int:
        return min(len(_TOKEN.findall(p)) for p in paths[key])

    candidates = [k for k in (matched or groups) if not all(_related(p) for p in paths[k])]
    if not candidates:
        return []
    top_depth = min(depth(k) for k in candidates)
    top = [k for k in candidates if depth(k) == top_depth]
    if len(top) != 1:
        return []  # several products side by side: none is the page's for sure
    return _one_representation(groups[top[0]], paths[top[0]])


def _one_representation(items: list[dict], owners: list[str]) -> list[dict]:
    """One product described under several owners (page state and a component's props)
    is read from one of them, else its boxes would count twice: the one that measures
    most, the shallowest of those. Genuinely separate boxes share an owner and stay."""
    by_owner: dict[str, list[dict]] = {}
    for it, owner in zip(items, owners, strict=True):
        by_owner.setdefault(owner, []).append(it)
    if len(by_owner) == 1:
        return items
    best = min(
        by_owner,
        key=lambda o: (
            -sum(1 for it in by_owner[o] if _measures(it["node"])),
            len(_TOKEN.findall(o)),
        ),
    )
    return by_owner[best]


def _box_list(path: str) -> str | None:
    """The list a box belongs to ("p.packages" for "p.packages[1].dimensions"), or the
    package object itself; None outside packages."""
    root = package_root(path)
    if root is None:
        return None
    return re.sub(r"\[\d+\]$", "", root)


def _measures(node: dict) -> dict[str, str]:
    """label -> value text of one node, each value read with its own node's units."""
    out: dict[str, str] = {}
    for k, v in node.items():
        label = LABELS.get(_key(k))
        if label is not None and label not in out:
            text = _value_text(v, node)
            if text:
                out[label] = text
    return out


def _merged(items: list[dict]) -> dict[str, str]:
    """One box from a package and its nested parts (weight on the box, axes in its
    "dimensions", each with its own sibling unit): the shallower object's values win."""
    out: dict[str, str] = {}
    for item in sorted(items, key=lambda it: len(it.get("path", ""))):
        for label, text in _measures(item.get("node") or {}).items():
            out.setdefault(label, text)
    return out


def _context(path: str) -> str:
    """Path segments the spec-text tier must see in the label: a "weight" under
    "maximumLoad" is a carrying capacity, not the product's mass."""
    words = []
    for t in _TOKEN.findall(path):
        if t.startswith("["):
            continue
        human = re.sub(r"([a-z])([A-Z])", r"\1 \2", t).replace("_", " ").replace("-", " ")
        if spectext.LOAD_LABEL.search(human):
            words.append(human.lower())
    return " ".join(words)


def _emit(measures: dict[str, str], prefix: str, out: list, package: bool) -> None:
    for label, text in measures.items():
        if package and label == "volume":
            continue  # a box's volume is its a*b*c, not the product's true volume
        out.append((f"{prefix}{label}", text))


def pairs(
    state: list[dict], locale: str | None = None, main: dict[str, str | None] | None = None
) -> list[tuple[str, str]]:
    """`locale` is the page's: a weight written as text ("1,250 kg") is read with it.
    `main`: identifiers of the page's product from JSON-LD ({"gtin": ..., "sku": ...})."""
    out: list[tuple[str, str]] = []
    boxes: dict[str, list[dict]] = {}
    # The worker hit a limit: some boxes may be missing, so neither one box's size nor
    # a sum is the package (other values stay: each is complete on its own).
    truncated = any(isinstance(it, dict) and it.get("truncated") for it in state)
    for item in main_product(state, main):
        path = item.get("path", "")
        root = package_root(path)
        if root is not None and truncated:
            continue
        context = _context(path)
        if context:
            # e.g. "maximum load weight": the spec-text tier reads it as a capacity
            prefix = f"{context} {'package ' if root else ''}"
            _emit(_measures(item["node"]), prefix, out, package=bool(root))
        elif root is None:
            _emit(_measures(item["node"]), "", out, package=False)
        else:
            boxes.setdefault(root, []).append(item)
    if len(boxes) == 1:
        _emit(_merged(next(iter(boxes.values()))), "package ", out, package=True)
    elif boxes:
        # Several boxes: only their total mass is meaningful, not any one box's size.
        masses = []
        for items in boxes.values():
            box = _merged(items)
            text = box.get("gross weight") or box.get("weight")
            q = units.parse_quantity(text, "mass", locale) if text else None
            if q is None:
                return out  # one box without a weight: no total
            masses.append(q.value)
        out.append(("package total gross weight", _num(sum(masses), "kg")))
    return out
