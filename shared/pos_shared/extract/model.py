"""What extraction produces: candidates with provenance, never final decisions."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class OfferCandidate:
    price: float | None
    currency: str | None
    price_raw: str | None
    availability: str | None = None  # in_stock | out_of_stock | discontinued | preorder | ...
    condition: str | None = None  # new | used | refurbished | damaged
    seller: str | None = None
    price_kind: str = "item"  # item | unit_price | from_price | aggregate_low | older_cost
    method: str = "jsonld"
    confidence: float = 0.9
    # The date a price refers to when it is not the fetch's (a unit cost from a
    # reference source: "1869-01-01"), and how that date was found.
    as_of: str | None = None
    as_of_method: str | None = None


@dataclass
class MeasurementCandidate:
    basis: str  # product | package | unknown
    basis_method: str  # labelled | parser_rule | heuristic
    a_m: float | None = None
    b_m: float | None = None
    c_m: float | None = None
    mass_kg: float | None = None
    net_or_gross: str | None = None
    true_volume_m3: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    method: str = "jsonld"
    confidence: float = 0.8

    @property
    def has_size(self) -> bool:
        return self.a_m is not None or self.mass_kg is not None or self.true_volume_m3 is not None


@dataclass
class Extraction:
    extract_version: str
    title: str | None = None
    brand: str | None = None
    gtin: str | None = None
    mpn: str | None = None
    sku: str | None = None
    category_path: list[str] = field(default_factory=list)
    offers: list[OfferCandidate] = field(default_factory=list)
    measurements: list[MeasurementCandidate] = field(default_factory=list)
    pack_size: float | None = None
    flags: list[str] = field(default_factory=list)
    variants: int = 0  # number of variants seen (ProductGroup)
    sources: list[str] = field(default_factory=list)  # which tiers contributed

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
