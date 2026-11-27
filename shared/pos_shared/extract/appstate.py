"""Embedded application state (Next.js, Nuxt, hydration JSON) -> label/value pairs.

The worker keeps only the small objects of a page's state that describe a size,
with their path ("product.packageMeasurements[0]"). Here they become the same
label/value pairs a spec table gives, so the spec-text tier decides what they mean:
the path says whether a node describes the package, the key which axis or mass.
"""

import re
from typing import Any

from pos_shared import units

PACKAGE_PATH = re.compile(r"package|packaging|shipping|carton|parcel|box|verpackung|paket", re.I)
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


def _num(value: float, unit: str) -> str:
    """Four decimals: never read as a thousands group, in any locale ("1.2500 kg")."""
    return f"{value:.4f} {unit}"


def _key(k: str) -> str:
    return re.sub(r"[^a-z]", "", k.lower())


def _value_text(v: Any, node: dict) -> str | None:
    """'27.5600 kg' from {'text': '27.56 kg', 'value': 27.56}, {'value': 27.56, 'unit':
    'kg'} or a bare number with a sibling unit; '27,56 kg' from a plain string. A number
    without any unit is not guessed."""
    if isinstance(v, dict):
        text = v.get("text") or v.get("formatted") or v.get("display")
        unit = v.get("unit") or v.get("unitOfMeasure") or v.get("unitCode")
        value = v.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            # The number itself, with the unit from the text: state JSON writes "27.56 kg"
            # whatever the page's language, and "1.250 kg" read as German is 1250 kg.
            if not isinstance(unit, str) and isinstance(text, str):
                m = UNIT_IN_TEXT.search(text.strip())
                unit = m.group(0) if m else None
            if isinstance(unit, str):
                return _num(value, unit)
        if isinstance(text, str) and text.strip():
            return text
        return None
    if isinstance(v, str):
        return v if v.strip() else None
    if isinstance(v, int | float) and not isinstance(v, bool):
        unit = node.get("unit") or node.get("unitOfMeasure")
        return _num(v, unit) if isinstance(unit, str) else None
    return None


_TOKEN = re.compile(r"\[\d+\]|[^.\[\]]+")


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
    return "".join(
        t if t.startswith("[") or i == 0 else f".{t}" for i, t in enumerate(tokens[: end + 1])
    )


def _merged(items: list[dict]) -> dict:
    """One object from a package and its nested parts (weight on the box, axes in its
    "dimensions"): the shallower object's values win."""
    out: dict = {}
    for item in sorted(items, key=lambda it: len(it.get("path", ""))):
        for k, v in (item.get("node") or {}).items():
            out.setdefault(k, v)
    return out


def _emit(node: dict, prefix: str, out: list[tuple[str, str]], package: bool) -> None:
    for k, v in node.items():
        label = LABELS.get(_key(k))
        if label is None or (package and label == "volume"):
            continue  # a box's volume is its a*b*c, not the product's true volume
        text = _value_text(v, node)
        if text:
            out.append((f"{prefix}{label}", text))


def pairs(state: list[dict], locale: str | None = None) -> list[tuple[str, str]]:
    """`locale` is the page's: a weight written as text ("1,250 kg") is read with it."""
    out: list[tuple[str, str]] = []
    boxes: dict[str, list[dict]] = {}
    for item in state:
        root = package_root(item.get("path", ""))
        if root is None:
            _emit(item.get("node") or {}, "", out, package=False)
        else:
            boxes.setdefault(root, []).append(item)
    if len(boxes) == 1:
        _emit(_merged(next(iter(boxes.values()))), "package ", out, package=True)
    elif boxes:
        # Several boxes: only their total mass is meaningful, not any one box's size.
        masses = []
        for items in boxes.values():
            node = _merged(items)
            weight = next(
                (v for k, v in node.items() if _key(k) in ("weight", "grossweight")), None
            )
            text = _value_text(weight, node) if weight is not None else None
            q = units.parse_quantity(text, "mass", locale) if text else None
            if q is None:
                return out  # one box without a weight: no total
            masses.append(q.value)
        out.append(("package total gross weight", _num(sum(masses), "kg")))
    return out
