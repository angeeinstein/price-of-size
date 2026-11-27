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

PACKAGE_PATH = re.compile(r"package|packaging|shipping|carton|parcel|box|verpackung|paket", re.I)
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
    named = [i for i, t in enumerate(tokens) if not t.startswith("[") and PACKAGE_PATH.search(t)]
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
        if not t.startswith("[") and PACKAGE_PATH.search(t):
            break  # indexes below the package are its boxes, not products
        if t.startswith("[") and i and PRODUCT_LIST.search(tokens[i - 1]):
            end = i
    return _join(tokens[: end + 1]) if end is not None else ""


def _canon(key: str, value: str) -> str:
    v = value.strip().lower()
    return re.sub(r"\D", "", v).lstrip("0") if key.startswith("gtin") or key == "ean" else v


def _matches(item: dict, main: dict[str, str | None]) -> bool:
    ids = (item.get("owner") or {}).get("ids") or {}
    if not isinstance(ids, dict):
        return False
    wanted = {k: _canon(k, v) for k, v in main.items() if isinstance(v, str) and v.strip()}
    for k, v in ids.items():
        if not isinstance(v, str):
            continue
        kind = "gtin" if k.startswith("gtin") or k == "ean" else k
        if kind in wanted and wanted[kind] and _canon(kind, v) == wanted[kind]:
            return True
    return False


def _related(path: str) -> bool:
    return any(RELATED_PATH.search(t) for t in _TOKEN.findall(path) if not t.startswith("["))


def main_product(state: list[dict], main: dict[str, str | None] | None = None) -> list[dict]:
    """The nodes of the page's own product (see the module docstring)."""
    groups: dict[str, list[dict]] = {}
    for it in state:
        if not isinstance(it, dict) or not isinstance(it.get("node"), dict):
            continue
        owner = _owner(it)
        # A related block below the owner holds other products (also ones without
        # identifiers, which would otherwise count as the page's own).
        if _related(it.get("path", "")[len(owner) :]):
            continue
        groups.setdefault(owner, []).append(it)
    matched = [o for o, its in groups.items() if main and any(_matches(it, main) for it in its)]
    if len(matched) == 1:
        return groups[matched[0]]
    candidates = [o for o in (matched or groups) if not _related(o)]
    if not candidates:
        return []
    depth = min(len(_TOKEN.findall(o)) for o in candidates)
    top = [o for o in candidates if len(_TOKEN.findall(o)) == depth]
    if len(top) != 1:
        return []  # several products side by side: none is the page's for sure
    return groups[top[0]]


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
    for item in main_product(state, main):
        path = item.get("path", "")
        root = package_root(path)
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
