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


def pairs(state: list[dict]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    package_nodes = [s for s in state if PACKAGE_PATH.search(s.get("path", ""))]
    for item in state:
        node = item.get("node") or {}
        is_package = item in package_nodes
        if is_package and len(package_nodes) > 1:
            continue  # several boxes: only their total mass is meaningful (below)
        prefix = "package " if is_package else ""
        for k, v in node.items():
            label = LABELS.get(_key(k))
            if label is None or (is_package and label == "volume"):
                continue  # a box's volume is its a*b*c, not the product's true volume
            text = _value_text(v, node)
            if text:
                out.append((f"{prefix}{label}", text))
    if len(package_nodes) > 1:
        masses = []
        for item in package_nodes:
            node = item.get("node") or {}
            weight = next(
                (v for k, v in node.items() if _key(k) in ("weight", "grossweight")), None
            )
            text = _value_text(weight, node) if weight is not None else None
            q = units.parse_quantity(text, "mass", "en") if text else None
            if q is None:
                masses = []
                break  # one box without a weight: no total
            masses.append(q.value)
        if masses:
            out.append(("package total gross weight", _num(sum(masses), "kg")))
    return out
