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
        ({"price": "99999"}, [], "x", "placeholder_price"),
        ({"price": "12", "availability": "OutOfStock"}, [], "x", "unavailable"),
        (
            {  # only a base price (per kg) is given: not the item price
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


# --- regressions from the independent review (each input was confirmed broken) --------


def _offer_ld(**offer):
    return {"@type": "Product", "name": "x", "offers": {"@type": "Offer", **offer}}


@pytest.mark.parametrize(
    "doc",
    [
        '{"@type": "BreadcrumbList", "itemListElement": [{"position": "1", "name": "a"},'
        ' {"position": 2, "name": "b"}, {"position": null, "name": "c"}]}',
        json.dumps(_offer_ld(price="5", priceCurrency="EUR", priceSpecification=["x", 3])),
        '{"@type": null, "name": "x"}',
        '{"@type": ["Product", null, 7], "name": "x", "offers": {"price": "5"}}',
        "[" * 5000 + "]" * 5000,
        '{"@type": "Product", "offers": "free"}',
    ],
)
def test_hostile_json_ld_never_raises(doc):
    extract_evidence(evidence([doc]))


@pytest.mark.parametrize("price", [float("nan"), float("inf"), True, "NaN"])
def test_non_finite_and_boolean_prices_are_no_prices(price):
    ex = extract_evidence(evidence([_offer_ld(price=price, priceCurrency="EUR")]))
    assert ex.offers[0].price is None
    doc = '{"@type": "Product", "name": "x", "offers": {"price": 1e400, "priceCurrency": "EUR"}}'
    assert extract_evidence(evidence([doc])).offers[0].price is None  # json reads inf


def test_negative_price_is_kept_and_flagged():
    ex = extract_evidence(evidence([_offer_ld(price="-5,00", priceCurrency="EUR")]))
    assert ex.offers[0].price == -5.0 and "placeholder_price" in ex.flags


def test_base_price_beside_the_item_price_is_not_a_unit_price():
    spec = {
        "@type": "UnitPriceSpecification",
        "price": "4.00",
        "priceCurrency": "EUR",
        "referenceQuantity": {"value": 1, "unitCode": "KGM"},
    }
    ex = extract_evidence(
        evidence([_offer_ld(price="19.99", priceCurrency="EUR", priceSpecification=spec)])
    )
    assert ex.offers[0].price == 19.99 and ex.offers[0].price_kind == "item"
    assert "unit_price" not in ex.flags


def test_strikethrough_price_is_not_the_price():
    specs = [
        {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/StrikethroughPrice",
         "price": "99.00"},
        {"@type": "UnitPriceSpecification", "price": "79.00"},
    ]  # fmt: skip
    ex = extract_evidence(evidence([_offer_ld(priceCurrency="EUR", priceSpecification=specs)]))
    assert ex.offers[0].price == 79.0


def test_aggregate_offer_details():
    ld = {
        "@type": "Product",
        "name": "x",
        "offers": {
            "@type": "AggregateOffer",
            "lowPrice": 0,
            "priceCurrency": "EUR",
            "offers": [{"@type": "Offer", "price": "12.00"}],
        },
    }
    ex = extract_evidence(evidence([ld]))
    low, nested = ex.offers
    assert low.price == 0.0  # zero is a value (and a placeholder), not "missing"
    assert nested.currency == "EUR"  # inherited from the AggregateOffer


def test_main_product_is_the_page_entity_not_a_nested_one():
    ld = {
        "@type": "Product",
        "name": "Sofa",
        "review": {
            "@type": "Review",
            "itemReviewed": {"@type": "Product", "name": "Kissen", "offers": {"price": "9"}},
        },
    }
    assert extract_evidence(evidence([ld])).title == "Sofa"


def test_gtin_keys_skip_placeholders_and_variants_as_dict():
    ld = {"@type": "Product", "name": "x", "gtin13": "n/a", "gtin": "4006381333931"}
    assert extract_evidence(evidence([ld])).gtin == "4006381333931"
    group = {"@type": "ProductGroup", "name": "x", "hasVariant": {"@type": "Product"}}
    assert extract_evidence(evidence([group])).variants == 1


def test_json_ld_quantities():
    ld = {
        "@type": "Product",
        "name": "x",
        "weight": "1,250 kg",  # a German page: 1.25 kg
        "width": {"value": 40, "unitCode": "cm"},  # not a UN/CEFACT code, still clear
    }
    (m,) = extract_evidence(evidence([ld], lang="de")).measurements
    assert m.mass_kg == pytest.approx(1.25) and m.a_m == pytest.approx(0.4)


def test_spec_blocks_keep_line_boundaries():
    blocks = ["<ul><li>Gewicht: 3 kg</li><li>Maße: 10 x 20 x 30 cm</li></ul>"]
    (m,) = extract_evidence(evidence(spec_blocks=blocks)).measurements
    assert m.mass_kg == 3.0 and m.a_m == pytest.approx(0.3)


def test_load_capacity_is_not_the_mass_and_swiss_masse_are_dimensions():
    rows = (
        "<table><tr><th>Max. Benutzergewicht</th><td>120 kg</td></tr>"
        "<tr><th>Belastbarkeit</th><td>150 kg</td></tr>"
        "<tr><th>Gewicht</th><td>8 kg</td></tr>"
        "<tr><th>Masse</th><td>60 x 60 x 100 cm</td></tr></table>"
    )
    (m,) = extract_evidence(evidence(tables=[rows])).measurements
    assert m.mass_kg == 8.0 and m.a_m == pytest.approx(1.0)


def test_without_packaging_is_the_product():
    rows = "<table><tr><th>Gewicht ohne Verpackung</th><td>8 kg</td></tr></table>"
    (m,) = extract_evidence(evidence(tables=[rows])).measurements
    assert m.basis == "product"
