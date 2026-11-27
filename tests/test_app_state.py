"""Embedded application state (hydration JSON): package data shops only render
client-side. The fixtures mimic the structure of real shop pages, not their content."""

import json

import pytest

from pos_shared.extract.pipeline import extract_evidence
from pos_worker.fetch import blocks, snippets


def _page(state: dict, script_type="text/hydrate") -> str:
    ld = {
        "@type": "Product",
        "name": "Regal 100x175 cm",
        "width": "100 cm",
        "height": "175 cm",
        "depth": "36 cm",
        "offers": {"@type": "Offer", "price": "129", "priceCurrency": "EUR"},
    }
    return (
        '<html lang="de-AT"><head><script type="application/ld+json">'
        + json.dumps(ld)
        + f'</script></head><body><script type="{script_type}">'
        + json.dumps(state)
        + "</script></body></html>"
    )


def _product(packages, media=True) -> dict:
    product = {"name": "Regal", "price": 129, "packageMeasurements": packages}
    if media:
        product["mediaList"] = [
            {"content": {"url": "https://x/i.jpg", "alt": "a", "height": 2000, "width": 2000}}
        ]
        product["mainImage"] = {"height": 4000, "width": 4000, "aspectRatio": {"x": 1, "y": 1}}
    return {"0": {"loaderData": {"product": product}}}


def _box(l, w, h, kg):  # noqa: E741
    return {
        "length": {"text": f"{l} cm", "value": l},
        "width": {"text": f"{w} cm", "value": w},
        "height": {"text": f"{h} cm", "value": h},
        "volume": {"text": "72.3 l", "value": 72.3},
        "weight": {"text": f"{kg} kg", "value": kg},
    }


def test_package_from_hydration_state_and_no_image_noise():
    snip = snippets.capture(_page(_product([_box(202, 39, 9, 27.56)])), "https://shop.example/p/1")
    assert [n["path"] for n in snip["app_state"]] == ["0.loaderData.product.packageMeasurements[0]"]
    ex = extract_evidence({"status": 200, "verdict": {"kind": "ok"}, "snippets": snip})
    by = {m.basis: m for m in ex.measurements}
    assert (by["unknown"].a_m, by["unknown"].b_m, by["unknown"].c_m) == pytest.approx(
        (1.75, 1.0, 0.36)
    )  # the product, from JSON-LD
    pkg = by["package"]
    assert (pkg.a_m, pkg.b_m, pkg.c_m, pkg.mass_kg) == pytest.approx((2.02, 0.39, 0.09, 27.56))
    assert pkg.true_volume_m3 is None  # a box's volume is not the product's volume
    assert "app_state" in ex.sources


def test_several_boxes_give_a_total_mass_but_no_box_size():
    state = _product([_box(202, 39, 9, 27.56), _box(120, 40, 5, 10.0)])
    snip = snippets.capture(_page(state), "https://shop.example/p/2")
    ex = extract_evidence({"status": 200, "verdict": {"kind": "ok"}, "snippets": snip})
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert pkg.mass_kg == pytest.approx(37.56) and pkg.a_m is None


@pytest.mark.parametrize("script_type", ["application/json", "text/hydrate"])
def test_next_data_and_json_scripts(script_type):
    state = {"props": {"pageProps": {"item": {"netWeight": "8,5 kg", "dimensions": "120 x 60 cm"}}}}
    snip = snippets.capture(_page(state, script_type), "https://shop.example/p/3")
    ex = extract_evidence({"status": 200, "verdict": {"kind": "ok"}, "snippets": snip})
    masses = [m.mass_kg for m in ex.measurements if m.mass_kg]
    assert masses == [pytest.approx(8.5)]


def test_numbers_without_units_and_broken_json_are_ignored():
    page = (
        '<html><script type="application/json">{"weight": 12, "height": 30}</script>'
        '<script type="application/json">{not json</script></html>'
    )
    snip = snippets.capture(page, "https://shop.example/p/4")
    ex = extract_evidence({"status": 200, "verdict": {"kind": "ok"}, "snippets": snip})
    assert ex.measurements == []


def test_german_bot_check_page_is_a_block():
    page = "<html><title>Sichere Verbindung wird überprüft</title><body>...</body></html>"
    assert blocks.classify(403, page).kind == "blocked"


def test_state_numbers_are_not_read_as_thousands_on_german_pages():
    state = _product(
        [
            {
                "weight": {"text": "1.250 kg", "value": 1.25},
                "height": {"value": 9, "unit": "cm"},
                "width": {"text": "39 cm", "value": 39},
            }
        ]
    )
    snip = snippets.capture(_page(state), "https://shop.example/p/5")
    ex = extract_evidence({"status": 200, "verdict": {"kind": "ok"}, "snippets": snip})
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert pkg.mass_kg == pytest.approx(1.25) and pkg.a_m == pytest.approx(0.39)


def _extract(state: dict, url="https://shop.example/p/6"):
    snip = snippets.capture(_page(state), url)
    return snip, extract_evidence({"status": 200, "verdict": {"kind": "ok"}, "snippets": snip})


def test_a_product_with_its_own_url_keeps_its_sizes():
    """A page URL is not a media descriptor; an image URL is."""
    product = {
        "url": "https://shop.example/p/7",
        "weight": "12 kg",
        "package": {"height": "100 cm", "width": "50 cm", "depth": "20 cm", "weight": "13 kg"},
        "image": {"url": "https://cdn.example/7.jpg", "width": 800, "height": 600},
        "hero": {"href": "https://cdn.example/7.webp?w=2", "width": "800 px", "weight": "1 kg"},
    }
    snip, ex = _extract({"product": product})
    assert [n["path"] for n in snip["app_state"]] == ["product", "product.package"]
    by = {m.basis: m for m in ex.measurements}
    assert by["package"].mass_kg == pytest.approx(13) and by["package"].a_m == pytest.approx(1)


def test_one_package_with_nested_dimensions_is_one_box():
    state = {
        "product": {
            "package": {
                "weight": "10 kg",
                "dimensions": {"height": "100 cm", "width": "50 cm", "depth": "20 cm"},
            }
        }
    }
    _, ex = _extract(state)
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert (pkg.a_m, pkg.b_m, pkg.c_m, pkg.mass_kg) == pytest.approx((1.0, 0.5, 0.2, 10.0))


def test_boxes_with_nested_parts_and_text_weights_in_the_page_locale():
    state = {
        "product": {
            "packages": [
                {"weight": "1,250 kg", "dimensions": {"height": "10 cm", "width": "20 cm"}},
                {"weight": "2,250 kg", "dimensions": {"height": "30 cm", "width": "40 cm"}},
            ]
        }
    }
    _, ex = _extract(state)  # lang="de-AT": decimal commas
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert pkg.mass_kg == pytest.approx(3.5) and pkg.a_m is None


def test_unit_codes_are_translated():
    box = {
        "weight": {"value": 10, "unitCode": "KGM"},
        "height": {"value": 100, "unitCode": "CMT", "text": "100 cm"},
        "width": {"value": 500, "unitCode": "MMT"},
        "depth": {"value": 2, "unitCode": "DMT"},
    }
    _, ex = _extract({"product": {"package": box}})
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert (pkg.a_m, pkg.b_m, pkg.c_m, pkg.mass_kg) == pytest.approx((1.0, 0.5, 0.2, 10.0))


def _pkg(kg):
    return {"weight": f"{kg} kg", "height": "100 cm", "width": "50 cm", "depth": "20 cm"}


def test_recommended_products_do_not_join_the_page_product():
    state = {
        "product": {"package": _pkg(10)},
        "recommendations": [{"package": _pkg(2)}, {"package": _pkg(3)}],
        "relatedProducts": {"items": [{"package": _pkg(4)}]},
    }
    _, ex = _extract(state)
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert pkg.mass_kg == pytest.approx(10) and pkg.a_m == pytest.approx(1.0)


def test_the_product_matching_the_json_ld_is_chosen():
    # JSON-LD of _page has no identifiers: add a gtin and put two products side by side.
    state = {
        "products": [
            {"sku": "A-1", "gtin13": "4006381333931", "package": _pkg(10)},
            {"sku": "B-2", "gtin13": "4006381333948", "package": _pkg(2)},
        ]
    }
    html = _page(state).replace('"name": "Regal', '"gtin13": "4006381333948", "name": "Regal', 1)
    snip = snippets.capture(html, "https://shop.example/p/8")
    assert {n["owner"]["path"] for n in snip["app_state"]} == {"products[0]", "products[1]"}
    ex = extract_evidence({"status": 200, "verdict": {"kind": "ok"}, "snippets": snip})
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert pkg.mass_kg == pytest.approx(2)


def test_several_products_without_a_match_give_nothing():
    state = {"hits": [{"package": _pkg(10)}, {"package": _pkg(2)}]}
    _, ex = _extract(state)
    assert not [m for m in ex.measurements if m.basis == "package"]


def test_a_load_rating_is_not_the_product_mass():
    state = {"product": {"maximumLoad": {"weight": "100 kg", "info": "x"}, "netWeight": "10 kg"}}
    _, ex = _extract(state)
    assert [m.mass_kg for m in ex.measurements if m.mass_kg] == [pytest.approx(10)]


def test_numbered_boxes_belong_to_their_product():
    state = {
        "product": {
            "sku": "A",
            "weight": "8 kg",
            "packages": [
                {"itemNumber": "A-1", "weight": "10 kg", "height": "100 cm"},
                {"itemNumber": "A-2", "weight": "2 kg", "height": "50 cm"},
            ],
        },
        "relatedProducts": [{"sku": "B", "package": _pkg(3)}],
    }
    html = _page(state).replace('"name": "Regal', '"sku": "A", "name": "Regal', 1)
    snip = snippets.capture(html, "https://shop.example/p/9")
    assert {n["owner"]["path"] for n in snip["app_state"]} == {"product", "relatedProducts[0]"}
    ex = extract_evidence({"status": 200, "verdict": {"kind": "ok"}, "snippets": snip})
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert pkg.mass_kg == pytest.approx(12)


def test_nested_parts_keep_their_own_units():
    box = {
        "weight": 10,
        "unit": "kg",
        "dimensions": {"height": 100, "width": 50, "depth": 20, "unit": "cm"},
    }
    _, ex = _extract({"product": {"package": box}})
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert (pkg.a_m, pkg.b_m, pkg.c_m, pkg.mass_kg) == pytest.approx((1.0, 0.5, 0.2, 10.0))


def test_a_recommendation_without_identifiers_does_not_displace_the_product():
    state = {
        "product": {"sku": "A", "package": _pkg(10)},
        "recommendations": [{"package": _pkg(2)}],
    }
    _, ex = _extract(state)  # the JSON-LD has no sku: no match, fallback ranking
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert pkg.mass_kg == pytest.approx(10)


def test_a_capture_cut_short_gives_no_package_total():
    state = {"product": {"netWeight": "5 kg", "packages": [{"weight": "1 kg"} for _ in range(60)]}}
    snip, ex = _extract(state)
    assert snip["app_state"][-1] == {"truncated": True}
    assert not [m for m in ex.measurements if m.basis == "package"]
    assert [m.mass_kg for m in ex.measurements if m.mass_kg] == [pytest.approx(5)]
    # within the limit: the full total
    state["product"]["packages"] = state["product"]["packages"][:30]
    snip, ex = _extract(state)
    assert not any(it.get("truncated") for it in snip["app_state"])
    pkg = next(m for m in ex.measurements if m.basis == "package")
    assert pkg.mass_kg == pytest.approx(30)
