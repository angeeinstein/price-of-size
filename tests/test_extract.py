"""Extraction tiers over realistic (synthetic) raw evidence."""

import json

import pytest

from pos_shared.extract.pipeline import extract_evidence


def evidence(
    json_ld=(),
    tables=(),
    dls=(),
    spec_blocks=(),
    price_blocks=(),
    title=None,
    lang="de",
    verdict="ok",
):
    return {
        "status": 200,
        "verdict": {"kind": verdict, "reason": None},
        "snippets": {
            "lang": lang,
            "title": title,
            "json_ld": [json.dumps(j) if not isinstance(j, str) else j for j in json_ld],
            "tables": list(tables),
            "definition_lists": list(dls),
            "spec_blocks": list(spec_blocks),
            "price_blocks": list(price_blocks),
        },
    }


FURNITURE_LD = {
    "@context": "https://schema.org",
    "@graph": [
        {
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 2, "name": "Kleiderschränke"},
                {"@type": "ListItem", "position": 1, "name": "Schlafzimmer"},
            ],
        },
        {
            "@type": "Product",
            "name": "Kleiderschrank, weiß, 100x60x201 cm",
            "brand": {"@type": "Brand", "name": "Example Home"},
            "gtin13": "4 012345 678901",
            "sku": "803.540.23",
            "weight": {"@type": "QuantitativeValue", "value": 120, "unitCode": "LBR"},
            "offers": {
                "@type": "Offer",
                "price": "299.00",
                "priceCurrency": "EUR",
                "availability": "https://schema.org/InStock",
                "itemCondition": "https://schema.org/NewCondition",
            },
        },
    ],
}
FURNITURE_TABLES = [
    "<table><tr><th>Breite</th><td>100 cm</td></tr><tr><th>Tiefe</th><td>60 cm</td></tr>"
    "<tr><th>Höhe</th><td>201 cm</td></tr></table>",
    "<table><tr><th>Paket Länge</th><td>205 cm</td></tr>"
    "<tr><th>Paket Breite</th><td>60 cm</td></tr><tr><th>Paket Höhe</th><td>12 cm</td></tr>"
    "<tr><th>Paket Gewicht</th><td>58,40 kg</td></tr></table>",
]


def test_furniture_shop_product_and_package():
    ex = extract_evidence(evidence([FURNITURE_LD], FURNITURE_TABLES))
    assert ex.title.startswith("Kleiderschrank") and ex.brand == "Example Home"
    assert ex.gtin == "4012345678901" and ex.sku == "803.540.23"
    assert ex.category_path == ["Schlafzimmer", "Kleiderschränke"]  # by position
    (offer,) = ex.offers
    assert (offer.price, offer.currency, offer.availability, offer.condition) == (
        299.0,
        "EUR",
        "in_stock",
        "new",
    )
    by = {(m.method, m.basis): m for m in ex.measurements}
    assert by[("jsonld", "unknown")].mass_kg == pytest.approx(120 * 0.45359237)
    product = by[("spec_table", "unknown")]
    assert (product.a_m, product.b_m, product.c_m) == pytest.approx((2.01, 1.0, 0.6))
    package = by[("spec_table", "package")]
    assert (package.a_m, package.b_m, package.c_m) == pytest.approx((2.05, 0.6, 0.12))
    assert package.mass_kg == pytest.approx(58.4) and package.basis_method == "labelled"
    assert ex.flags == [] and set(ex.sources) == {"jsonld", "spec_table"}


def test_electronics_spec_table_net_and_gross():
    rows = (
        "<table><tr><td>Abmessungen (BxHxT)</td><td>715x1070x45mm</td></tr>"
        "<tr><td>Gewicht</td><td>9.8kg</td></tr>"
        "<tr><td>Bruttogewicht (Versand)</td><td>14,2kg</td></tr>"
        "<tr><td>Preis</td><td>ab € 1.299,–</td></tr></table>"
    )
    ex = extract_evidence(evidence(tables=[rows], title="Fernseher 49 Zoll"))
    by = {m.basis: m for m in ex.measurements}
    tv = by["unknown"]
    assert (tv.a_m, tv.b_m, tv.c_m) == pytest.approx((1.07, 0.715, 0.045))
    assert tv.mass_kg == pytest.approx(9.8)
    assert by["package"].mass_kg == pytest.approx(14.2)
    assert by["package"].net_or_gross == "gross"


def test_variants_aggregate_offers_and_marketplace():
    ld = {
        "@type": "ProductGroup",
        "name": "Sofa",
        "hasVariant": [
            {
                "@type": "Product",
                "name": "Sofa grau",
                "offers": {"@type": "Offer", "price": 499, "priceCurrency": "EUR"},
            },
            {
                "@type": "Product",
                "name": "Sofa blau",
                "offers": {"@type": "Offer", "price": 549, "priceCurrency": "EUR"},
            },
        ],
    }
    ex = extract_evidence(evidence([ld]))
    assert ex.variants == 2 and ex.offers[0].price == 499
    agg = {
        "@type": "Product",
        "name": "Monitor",
        "offers": {
            "@type": "AggregateOffer",
            "lowPrice": "189.90",
            "highPrice": "260",
            "priceCurrency": "EUR",
            "offerCount": 14,
        },
    }
    ex = extract_evidence(evidence([agg]))
    assert ex.offers[0].price_kind == "aggregate_low" and "aggregate_low_price" in ex.flags


@pytest.mark.parametrize(
    ("ld", "blocks", "title", "flag"),
    [
        ({"price": "0.01"}, [], "x", "placeholder_price"),
        ({"price": "9999"}, [], "x", "placeholder_price"),
        ({"price": "12", "availability": "OutOfStock"}, [], "x", "unavailable"),
        (
            {
                "price": "1.99",
                "priceSpecification": {
                    "@type": "UnitPriceSpecification",
                    "price": "3.98",
                    "referenceQuantity": {"value": 1, "unitCode": "KGM"},
                },
            },
            [],
            "x",
            "unit_price",
        ),
        ({"price": "29"}, ["nur 29 € / Monat"], "x", "monthly_price"),
        ({"price": "29"}, ["29,00 € zzgl. MwSt."], "x", "net_price"),
        ({"price": "199", "itemCondition": "UsedCondition"}, [], "x", "condition_used"),
        ({"price": "5.49"}, [], "Mineralwasser 6 x 1,5 l", "pack"),
    ],
)
def test_price_traps_are_flagged(ld, blocks, title, flag):
    offer = {"@type": "Offer", "priceCurrency": "EUR", **ld}
    ex = extract_evidence(
        evidence([{"@type": "Product", "name": title, "offers": offer}], price_blocks=blocks)
    )
    assert flag in ex.flags, ex.flags


def test_pack_size_from_title():
    ex = extract_evidence(
        evidence(
            [
                {
                    "@type": "Product",
                    "name": "Teelichter 100er-Pack",
                    "offers": {"price": "4.99", "priceCurrency": "EUR"},
                }
            ]
        )
    )
    assert ex.pack_size == 100


def test_broken_and_missing_json_ld():
    broken = '{"@type": "Product", "name": "Lamp",\n "offers": {"price": "19.99",}, }'
    ex = extract_evidence(evidence([broken]))
    assert ex.title == "Lamp" and ex.offers[0].price == 19.99
    ex = extract_evidence(evidence(["not json at all"], title="Fallback title"))
    assert ex.title == "Fallback title" and ex.offers == [] and "no_price" not in ex.flags


def test_numbers_without_units_are_not_guessed():
    ld = {"@type": "Product", "name": "x", "weight": {"value": 12}, "width": "40"}
    ex = extract_evidence(evidence([ld]))
    assert ex.measurements == []


def test_additional_property_and_volume():
    ld = {
        "@type": "Product",
        "name": "Kühlschrank",
        "additionalProperty": [
            {"@type": "PropertyValue", "name": "Nutzinhalt", "value": "250", "unitText": "l"},
            {"@type": "PropertyValue", "name": "Abmessungen", "value": "60 x 185 x 65 cm"},
        ],
    }
    ex = extract_evidence(evidence([ld]))
    (m,) = ex.measurements
    assert m.true_volume_m3 == pytest.approx(0.25)
    assert (m.a_m, m.b_m, m.c_m) == pytest.approx((1.85, 0.65, 0.6))


def test_blocked_fetch_is_flagged():
    assert "fetch_blocked" in extract_evidence(evidence(verdict="blocked")).flags


@pytest.mark.parametrize(
    ("title", "pack"),
    [
        ("Kleiderschrank, weiß, 100x60x201 cm", None),  # dimensions, not a multipack
        ("Mineralwasser 6 x 1,5 l", 6),
        ("Joghurt 4 x 125 g", 4),
        ("Schrauben 200 Stück", 200),
        ("Glühbirnen 2er-Set", 2),
        ("Fernseher 55 Zoll", None),
    ],
)
def test_pack_size_rules(title, pack):
    from pos_shared.extract.price_rules import pack_size

    assert pack_size(title) == pack
