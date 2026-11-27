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
