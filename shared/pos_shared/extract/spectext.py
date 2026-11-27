"""Tier 3: spec tables and spec text -> label/value pairs -> size candidates.

Handles German and English labels, decimal comma vs point, mm/cm/m/inch,
g/kg/lb/oz, net/gross and product/package wording (see pos_shared.units).
"""

import re
from collections import defaultdict

from lxml import html as lxml_html

from pos_shared import units
from pos_shared.extract.model import MeasurementCandidate

MASS_LABEL = re.compile(r"gewicht|weight|masse\b|\bmass\b", re.I)
VOLUME_LABEL = re.compile(r"inhalt|volumen|fassungsverm|füllmenge|capacity|volume\b", re.I)
DIMENSION_LABEL = re.compile(r"maße|masse\b|abmessung|dimension|größe\b|size\b|format", re.I)
# What a thing can carry, not what it weighs: "Max. Benutzergewicht", "Tragkraft",
# "Belastbarkeit", "max. weight capacity", "zulässiges Gesamtgewicht", "payload".
LOAD_LABEL = re.compile(
    r"benutzer|belastbar|belastung|trag(?:kraft|last|fähig|faehig)|capacity|\bmax|zulässig|"
    r"zulaessig|payload|\bload\b|nutzlast|zuladung",
    re.I,
)
# Elements that end a line of text (lxml's text_content() drops the boundary).
LINE_BREAKS = {"br", "li", "p", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd"}
MAX_PAIRS = 400


def pairs_from_html(fragments: list[str]) -> list[tuple[str, str]]:
    """Label/value pairs from table rows, definition lists and 'Label: value' lines."""
    out: list[tuple[str, str]] = []
    for frag in fragments:
        try:
            root = lxml_html.fragment_fromstring(frag, create_parent="div")
        except Exception:  # noqa: BLE001 - broken markup: skip this fragment
            continue
        for tr in root.iter("tr"):
            cells = [" ".join(c.text_content().split()) for c in tr if c.tag in ("th", "td")]
            if len(cells) >= 2 and cells[0] and cells[1]:
                out.append((cells[0].rstrip(":"), " ".join(cells[1:])))
        for dl in root.iter("dl"):
            label = None
            for el in dl:
                if el.tag == "dt":
                    label = " ".join(el.text_content().split()).rstrip(":")
                elif el.tag == "dd" and label:
                    out.append((label, " ".join(el.text_content().split())))
                    label = None
        if not root.findall(".//tr") and not root.findall(".//dl"):
            for el in root.iter():
                if el.tag in LINE_BREAKS and el is not root:
                    el.tail = "\n" + (el.tail or "")
            text = root.text_content()
            for line in re.split(r"[\n;•|]", text):
                if ":" in line:
                    label, _, value = line.partition(":")
                    if 0 < len(label.strip()) <= 60 and value.strip():
                        out.append((label.strip(), value.strip()))
        if len(out) >= MAX_PAIRS:
            break
    return out[:MAX_PAIRS]


def measurements(pairs: list[tuple[str, str]], locale: str | None) -> list[MeasurementCandidate]:
    """Group sizes by what they describe (product / package / unknown)."""
    groups: dict[str, MeasurementCandidate] = {}
    axes: dict[str, dict[str, float]] = defaultdict(dict)

    def group(label: str) -> MeasurementCandidate:
        basis = units.basis_of(label)
        if basis not in groups:
            groups[basis] = MeasurementCandidate(
                basis=basis,
                basis_method="labelled" if basis != "unknown" else "parser_rule",
                method="spec_table",
                confidence=0.85 if basis != "unknown" else 0.7,
            )
        return groups[basis]

    for label, value in pairs:
        if LOAD_LABEL.search(label) and not DIMENSION_LABEL.search(label):
            continue
        if MASS_LABEL.search(label):
            q = units.parse_quantity(value, "mass", locale)
            if q:
                g = group(label)
                ng = units.net_or_gross(label)
                # keep the first mass per group; a gross mass never replaces a net one
                if g.mass_kg is None or (ng == "net" and g.net_or_gross != "net"):
                    g.mass_kg, g.net_or_gross = q.value, ng
                    g.raw[label] = value
                continue
            # no mass: Swiss "Masse" means dimensions (Maße); fall through
        if VOLUME_LABEL.search(label):
            q = units.parse_quantity(value, "volume", locale)
            if q:
                g = group(label)
                g.true_volume_m3 = g.true_volume_m3 or q.value
                g.raw[label] = value
                continue
        dims = units.parse_dimensions(value, locale)
        if dims and (DIMENSION_LABEL.search(label) or units.basis_of(label) != "unknown"):
            g = group(label)
            if g.a_m is None:
                g.a_m, g.b_m, g.c_m = dims.a, dims.b, dims.c
                g.raw[label] = value
            continue
        axis = units.axis_of(label)
        if axis:
            q = units.parse_quantity(value, "length", locale)
            if q:
                basis = units.basis_of(label)
                axes[basis].setdefault(axis, q.value)
                group(label).raw[label] = value

    for basis, found in axes.items():
        g = groups[basis]
        if g.a_m is not None:
            continue  # a full dimension string wins over single axes
        values = sorted(found.values(), reverse=True)[:3]
        values += [None] * (3 - len(values))
        g.a_m, g.b_m, g.c_m = values
    return [g for g in groups.values() if g.has_size]
