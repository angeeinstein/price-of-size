"""Wikidata (SPARQL) as a source: unit costs and sizes of large things.

Aircraft, ships, trains, spacecraft, buildings: the upper end of the size range,
which no shop sells. One query per class (optionally with its subclasses) returns
one row per item with all its cost statements and size values; the raw response
is the evidence, and `records()` turns it into one extraction per item.

Values come normalised to SI by Wikidata itself (`psn:`: kg, m). A cost carries its
currency (the unit item, with its ISO 4217 code when it has one) and the date it
refers to (`point in time`, else the item's inception, first flight, launch or
service entry). Costs are nominal: never inflation-adjusted here, and an old cost
in a currency without a reference rate stays without a EUR value (flagged).
"""

import re
import statistics
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from pos_shared.extract import EXTRACT_VERSION
from pos_shared.extract.model import Extraction, MeasurementCandidate, OfferCandidate

ENDPOINT = "https://query.wikidata.org/sparql"
ACCEPT = "application/sparql-results+json"
ADAPTER = "wikidata"
QID = re.compile(r"^Q[1-9]\d{0,11}$")
ENTITY = re.compile(r"^https?://www\.wikidata\.org/entity/(Q[1-9]\d{0,11})$")
MAX_PAGE_SIZE = 5000

# cost (P2130) and price (P2284); the property is kept as the offer's method
COST_PROPERTIES = ("P2130", "P2284")
# a date the cost can refer to when its statement has none, best first
DATE_PROPERTIES = ("P571", "P606", "P619", "P729", "P1619")
SIZE_PROPERTIES = {
    "P2067": "mass",
    "P2043": "length",
    "P2049": "width",
    "P2048": "height",
    "P2386": "diameter",
}

_SEP = "|"
_FIELD = "~"


def build_query(class_id: str, subclasses: bool, limit: int, offset: int) -> str:
    """Items that are instances of `class_id` (or of a subclass) and have a cost.

    Built for the query service's 60 s limit: start from the items that have a cost
    (few) and check their class, not the other way round (listing every ship first
    times out); then aggregate each property per item in its own named subquery
    (Blazegraph's WITH ... INCLUDE, evaluated first), so the values of different
    properties are never multiplied with each other in one join."""
    if not QID.match(class_id):
        raise ValueError(f"not a Wikidata item id: {class_id!r}")
    path = "wdt:P31/wdt:P279*" if subclasses else "wdt:P31"
    has_cost = " UNION ".join(f"{{ ?item p:{p} [] }}" for p in COST_PROPERTIES)
    # VALUES, not UNION: a UNION branch is evaluated on its own, over every cost
    # statement in Wikidata, before it meets the page's items.
    cost_props = " ".join(f'("{p}" p:{p} psv:{p})' for p in COST_PROPERTIES)
    first = "INCLUDE %items . hint:Prior hint:runFirst true ."
    blocks = [
        f"""WITH {{
  SELECT DISTINCT ?item WHERE {{
    {has_cost}
    ?item {path} wd:{class_id} .
  }} ORDER BY ?item LIMIT {int(limit)} OFFSET {int(offset)}
}} AS %items""",
        f"""WITH {{
  SELECT ?item (GROUP_CONCAT(DISTINCT ?costv; separator="{_SEP}") AS ?costs) WHERE {{
    {first}
    VALUES (?prop ?pp ?pv) {{ {cost_props} }}
    ?item ?pp ?cst . ?cst ?pv ?cv .
    ?cv wikibase:quantityAmount ?ca ; wikibase:quantityUnit ?cu .
    OPTIONAL {{ ?cst pq:P585 ?cd }}
    OPTIONAL {{ ?cu wdt:P498 ?iso }}
    BIND(CONCAT(?prop, "{_FIELD}", STR(?ca), "{_FIELD}", STRAFTER(STR(?cu), "entity/"),
                "{_FIELD}", COALESCE(?iso, ""), "{_FIELD}", COALESCE(STR(?cd), "")) AS ?costv)
  }} GROUP BY ?item
}} AS %costs""",
    ]
    for p, name in SIZE_PROPERTIES.items():
        blocks.append(f"""WITH {{
  SELECT ?item (GROUP_CONCAT(DISTINCT STR(?v); separator="{_SEP}") AS ?{name}s) WHERE {{
    {first}
    OPTIONAL {{ ?item p:{p}/psn:{p}/wikibase:quantityAmount ?v }}
  }} GROUP BY ?item
}} AS %{name}""")
    for p in DATE_PROPERTIES:
        blocks.append(f"""WITH {{
  SELECT ?item (MIN(?d) AS ?{p}) WHERE {{
    {first}
    OPTIONAL {{ ?item wdt:{p} ?d }}
  }} GROUP BY ?item
}} AS %{p}""")
    blocks.append(f"""WITH {{
  SELECT ?item (SAMPLE(?l) AS ?label) WHERE {{
    {first}
    OPTIONAL {{ ?item rdfs:label ?l FILTER(LANG(?l) IN ("en", "de")) }}
  }} GROUP BY ?item
}} AS %labels""")
    names = [*SIZE_PROPERTIES.values()]
    # Every block has one row per item (OPTIONAL inside it): joining an empty named
    # solution set with OPTIONAL INCLUDE drops the bindings of the blocks after it.
    optional = "\n  ".join(f"INCLUDE %{n} ." for n in [*names, *DATE_PROPERTIES, "labels"])
    columns = " ".join([*(f"?{n}s" for n in names), *(f"?{p}" for p in DATE_PROPERTIES)])
    with_blocks = "\n".join(blocks)
    return f"""SELECT ?item ?label ?costs {columns}
{with_blocks}
WHERE {{
  INCLUDE %costs .
  {optional}
}}
ORDER BY ?item"""


def query_url(class_id: str, subclasses: bool, limit: int, offset: int, endpoint=ENDPOINT) -> str:
    return f"{endpoint}?{urlencode({'query': build_query(class_id, subclasses, limit, offset)})}"


@dataclass
class Record:
    qid: str
    url: str  # the item's page: one product per item (match key)
    extraction: Extraction
    raw: dict[str, Any] = field(default_factory=dict)


def _value(binding: dict, name: str) -> str | None:
    v = binding.get(name)
    if isinstance(v, dict) and isinstance(v.get("value"), str):
        return v["value"]
    return None


def _numbers(text: str | None) -> list[float]:
    out = []
    for part in (text or "").split(_SEP):
        try:
            x = float(part)
        except ValueError:
            continue
        if x > 0 and x == x and x != float("inf"):
            out.append(x)
    return out


def _date(text: str | None) -> str | None:
    """'1869-01-01T00:00:00Z' -> '1869-01-01'; Wikidata writes unknown parts as 00."""
    m = re.match(r"^(-?\d{1,6})-(\d\d)-(\d\d)", text or "")
    if not m:
        return None
    year, month, day = m.groups()
    if not (1 <= int(year) <= 9999):
        return None  # BCE or far-future: not a cost date
    return f"{int(year):04d}-{max(int(month), 1):02d}-{max(int(day), 1):02d}"


@dataclass
class _Cost:
    prop: str
    amount: float
    unit_qid: str
    iso: str | None
    as_of: str | None
    as_of_method: str


def _costs(binding: dict) -> list[_Cost]:
    fallback = next(
        ((p, d) for p in DATE_PROPERTIES if (d := _date(_value(binding, p)))), (None, None)
    )
    out = []
    for part in (_value(binding, "costs") or "").split(_SEP):
        fields = part.split(_FIELD)
        if len(fields) != 5:
            continue
        prop, amount, unit, iso, when = fields
        try:
            value = float(amount)
        except ValueError:
            continue
        if not value > 0 or value == float("inf"):
            continue
        as_of = _date(when)
        method = "point_in_time" if as_of else (f"item_{fallback[0]}" if fallback[0] else "none")
        out.append(
            _Cost(
                prop=prop,
                amount=value,
                unit_qid=unit,
                iso=iso.upper() if re.fullmatch(r"[A-Za-z]{3}", iso or "") else None,
                as_of=as_of or fallback[1],
                as_of_method=method,
            )
        )
    return out


def _measurement(binding: dict) -> MeasurementCandidate | None:
    vals = {name: _numbers(_value(binding, f"{name}s")) for name in SIZE_PROPERTIES.values()}
    axes = []
    for name in ("length", "width", "height"):
        if vals[name]:
            axes.append(statistics.median(vals[name]))
    if vals["diameter"]:
        d = statistics.median(vals["diameter"])
        # a cylinder (rocket, tank, tower): its diameter spans the other two axes
        axes += [d] * (min(2, 3 - len(axes)) if axes else 1)
    axes = sorted(axes, reverse=True)[:3]
    # several masses are variants (empty vs. take-off mass, surfaced vs. submerged):
    # the smallest is the object itself
    mass = min(vals["mass"]) if vals["mass"] else None
    if not axes and mass is None:
        return None
    a, b, c = (axes + [None, None, None])[:3]
    return MeasurementCandidate(
        basis="product",
        basis_method="labelled",
        a_m=a,
        b_m=b,
        c_m=c,
        mass_kg=mass,
        raw={k: _SEP.join(f"{x:g}" for x in v) for k, v in vals.items() if v},
        method="wikidata",
        confidence=0.85,
    )


def records(evidence: dict[str, Any], class_label: str | None = None) -> list[Record]:
    """One record per item of a SPARQL JSON response. Items without a usable cost
    or without any size are still returned (a cost alone is evidence too); the derive
    stage decides what counts."""
    body = evidence.get("body")
    bindings = (
        (((body or {}).get("results") or {}).get("bindings")) if isinstance(body, dict) else None
    )
    if not isinstance(bindings, list):
        return []
    out = []
    for b in bindings:
        if not isinstance(b, dict):
            continue
        item = _value(b, "item") or ""
        m = ENTITY.match(item)
        if not m:
            continue  # not a Wikidata item
        qid = m.group(1)
        ex = Extraction(extract_version=EXTRACT_VERSION, sources=[ADAPTER])
        ex.title = (_value(b, "label") or "")[:500] or None
        ex.sku = qid
        if class_label:
            ex.category_path = [class_label]
        costs = sorted(_costs(b), key=lambda c: c.as_of or "", reverse=True)
        for i, c in enumerate(costs):
            ex.offers.append(
                OfferCandidate(
                    price=c.amount,
                    currency=c.iso,
                    price_raw=f"{c.amount:g} {c.iso or c.unit_qid}",
                    price_kind="item" if i == 0 else "older_cost",
                    method=f"wikidata:{c.prop}",
                    confidence=0.8,
                    as_of=c.as_of,
                    as_of_method=c.as_of_method,
                )
            )
        if costs and costs[0].as_of is None:
            ex.flags.append("undated_cost")
        m = _measurement(b)
        if m is not None:
            ex.measurements.append(m)
        out.append(
            Record(
                qid=qid,
                url=f"https://www.wikidata.org/wiki/{qid}",
                extraction=ex,
                raw={"costs": [c.__dict__ for c in costs]},
            )
        )
    return out
