"""Target taxonomy import and classification by shop category mappings."""

import pytest
from sqlalchemy import select

from pos_coordinator.classify import shopmap, taxonomy
from pos_coordinator.models import Classification, Product, TaxonomyNode


def _google(names=None, version="2021-09-21") -> str:
    """A taxonomy file in Google's format, big enough to pass the sanity check."""
    names = names or {}
    lines = [f"# Google_Product_Taxonomy_Version: {version}"]
    lines += ["888 - Vehicles & Parts", "5614 - Vehicles & Parts > Vehicles"]
    lines += ["3395 - Vehicles & Parts > Vehicles > Aircraft"]
    lines += [f"436 - {names.get('436', 'Furniture')}"]
    lines += [f"6356 - {names.get('436', 'Furniture')} > Shelving"]
    lines += [f"{1000 + i} - Office Supplies > Item {i}" for i in range(120)]
    lines += ["922 - Office Supplies"]
    return "\n".join(lines) + "\n"


def _google_de() -> str:
    text = _google({"436": "Möbel"})
    return (
        text.replace("Vehicles & Parts", "Fahrzeuge & Teile")
        .replace("> Vehicles", "> Fahrzeuge")
        .replace("Aircraft", "Luftfahrzeuge")
        .replace("Shelving", "Regale")
    )


def test_taxonomy_files_are_checked():
    with pytest.raises(taxonomy.TaxonomyError):
        taxonomy.parse_google("<html>not found</html>")
    with pytest.raises(taxonomy.TaxonomyError):
        taxonomy.parse_google("1 - A\n2 - A > B\n")  # far too small
    with pytest.raises(taxonomy.TaxonomyError):
        taxonomy.parse_google(_google() + "7 - A >  > B\n")
    p = taxonomy.parse_google(_google())
    assert p.version == "2021-09-21" and p.rows["6356"] == ["Furniture", "Shelving"]


async def _import(s, **kw):
    return await taxonomy.import_google(s, {"en": _google(**kw), "de": _google_de()})


async def test_import_is_idempotent_and_keeps_dropped_categories(sessionmaker):
    async with sessionmaker() as s:
        first = await _import(s)
        assert first["added"] == len(taxonomy.CUSTOM) + 126
        await s.commit()
        again = await taxonomy.import_google(
            s, {"en": _google({"436": "Furniture & Fittings"}).replace("1119 - ", "# ")}
        )
        assert again["added"] == 0
        await s.commit()
        nodes = {n.external_id: n for n in (await s.execute(select(TaxonomyNode))).scalars()}
        shelving = nodes["6356"]
        assert shelving.path == "Furniture & Fittings > Shelving"  # renamed in place
        assert shelving.parent_id == nodes["436"].id and shelving.depth == 1
        assert shelving.names["de"]["path"] == "Möbel > Regale"  # from the first import
        assert "1119" in nodes  # dropped by Google: kept, classifications may use it
        space = nodes["pos-501"]
        assert space.is_custom and space.parent_id == nodes["5614"].id
        assert (
            space.names["de"]["path"]
            == "Fahrzeuge & Teile > Fahrzeuge > Raumfahrzeuge & Satelliten"
        )
        assert nodes["pos-101"].parent_id == nodes["pos-100"].id


async def _products(s, rows):
    for i, (source, path) in enumerate(rows):
        s.add(
            Product(
                match_key=f"url:https://{source}/p/{i}",
                source=source,
                title=f"p{i}",
                attributes={"category_path": path},
            )
        )
    await s.flush()


async def test_mappings_classify_and_follow_changes(sessionmaker):
    async with sessionmaker() as s:
        await _import(s)
        nodes = {n.external_id: n.id for n in (await s.execute(select(TaxonomyNode))).scalars()}
        shelf_path = ["Produkte", "Regale", "Regalsysteme"]
        await _products(
            s,
            [
                ("ikea", shelf_path),
                ("ikea", shelf_path),
                ("ikea", ["Produkte", "Leuchten"]),
                ("other", shelf_path),  # the same words in another shop: not mapped
                ("ikea", None),
            ],
        )
        paths = await shopmap.category_paths(s, unmapped_only=True)
        assert [(p["source"], p["path"], p["products"]) for p in paths] == [
            ("ikea", "Produkte > Regale > Regalsysteme", 2),
            ("ikea", "Produkte > Leuchten", 1),
            ("other", "Produkte > Regale > Regalsysteme", 1),
        ]
        await shopmap.set_mapping(s, "ikea", " Produkte >Regale >  Regalsysteme", nodes["6356"])
        assert await shopmap.classify(s) == {"classified": 2, "withdrawn": 0}
        assert await shopmap.classify(s) == {"classified": 0, "withdrawn": 0}  # settled

        async def current():
            rows = await s.execute(
                select(Product.title, Product.category_id, Classification.tier)
                .outerjoin(
                    Classification,
                    (Classification.product_id == Product.id) & Classification.is_current,
                )
                .order_by(Product.title)
            )
            return {r.title: (r.category_id, r.tier) for r in rows}

        state = await current()
        assert state["p0"] == state["p1"] == (nodes["6356"], "shop_mapping")
        assert state["p3"] == (None, None)

        # a person classified p1 by hand: a mapping never overrides that
        p1 = (await s.execute(select(Product).where(Product.title == "p1"))).scalar_one()
        await s.execute(
            Classification.__table__.update()
            .where(Classification.product_id == p1.id)
            .values(is_current=False)
        )
        s.add(
            Classification(
                product_id=p1.id,
                taxonomy_node_id=nodes["922"],
                tier="manual",
                confidence=1.0,
                classifier_version="manual",
            )
        )
        await s.flush()
        await shopmap.set_mapping(s, "ikea", "Produkte > Regale > Regalsysteme", nodes["436"])
        assert await shopmap.classify(s) == {"classified": 1, "withdrawn": 0}
        state = await current()
        assert state["p0"] == (nodes["436"], "shop_mapping")
        assert state["p1"][1] == "manual"

        await shopmap.set_mapping(s, "ikea", "Produkte > Regale > Regalsysteme", None)
        assert await shopmap.classify(s) == {"classified": 0, "withdrawn": 1}
        assert (await current())["p0"] == (None, None)
        history = (
            await s.execute(
                select(Classification.taxonomy_node_id, Classification.is_current)
                .join(Product, Product.id == Classification.product_id)
                .where(Product.title == "p0")
            )
        ).all()
        assert len(history) == 2 and not any(c for _, c in history)  # kept, not current


async def test_taxonomy_and_mapping_api(admin, sessionmaker):
    r = await admin.post(
        "/api/admin/taxonomy/import", json={"texts": {"en": _google(), "de": _google_de()}}
    )
    assert r.status_code == 200, r.text
    assert (
        await admin.post("/api/admin/taxonomy/import", json={"texts": {"en": "x"}})
    ).status_code == 422
    status = (await admin.get("/api/admin/taxonomy")).json()
    assert status["nodes"] == 126 + len(taxonomy.CUSTOM)
    hits = (
        await admin.get("/api/admin/taxonomy/search", params={"q": "regale", "locale": "de"})
    ).json()
    assert [h["external_id"] for h in hits] == ["6356"]
    async with sessionmaker() as s:
        await _products(s, [("ikea", ["Produkte", "Regale"])])
        await s.commit()
    paths = (await admin.get("/api/admin/category-paths", params={"unmapped": True})).json()
    assert [p["path"] for p in paths] == ["Produkte > Regale"]
    body = {"source": "ikea", "path": "Produkte > Regale", "external_id": "6356"}
    r = await admin.put("/api/admin/category-mappings", json=body)
    assert r.status_code == 200, r.text
    bad = {**body, "external_id": "nope"}
    assert (await admin.put("/api/admin/category-mappings", json=bad)).status_code == 422
    assert (await admin.get("/api/admin/category-paths", params={"unmapped": True})).json() == []
    mapped = (await admin.get("/api/admin/category-paths")).json()
    assert mapped[0]["node_path"] == "Furniture > Shelving"
    history = (
        await admin.get(
            "/api/admin/category-mappings/history",
            params={"source": "ikea", "path": "Produkte > Regale"},
        )
    ).json()
    assert [h["is_current"] for h in history] == [True]


async def test_paths_are_normalised_the_same_in_python_and_sql(sessionmaker):
    from sqlalchemy import text

    cases = [
        ["Home ", "Office  Chairs"],
        ["Möbel &\tWohnen", "  ", "Regale > Wandregale"],
        ["A>B", ">", "C"],
        ["x" * 600, "y" * 600],
    ]
    async with sessionmaker() as s:
        await _products(s, [("shop", c) for c in cases] + [("shop", "not a list")])
        rows = (
            await s.execute(
                text(f"SELECT p.title, {shopmap.PATH_SQL} AS path FROM product p ORDER BY p.title")
            )
        ).all()
    got = {r.title: r.path for r in rows}
    for i, c in enumerate(cases):
        assert got[f"p{i}"] == shopmap.path_string(c), c
    assert got["p0"] == "Home > Office Chairs"
    assert got["p1"] == "Möbel & Wohnen > Regale > Wandregale"
    assert got["p2"] == "A > B > C"
    assert len(got["p3"]) == shopmap.MAX_PATH_CHARS
    assert got["p4"] == ""
    assert shopmap.path_string("Home >  Office  Chairs") == "Home > Office Chairs"


async def test_the_rebuild_drill_classifies_again(app, sessionmaker):
    """Categories are derived: a rebuild drops them and must classify again from the
    (kept) mappings, or the drill would not reproduce the derived layer."""
    import hashlib
    import json
    from datetime import UTC, datetime

    import zstandard

    from pos_coordinator.derive import stage
    from pos_coordinator.models import RawFetch
    from pos_shared.ids import uuid7

    sm, store = app.state.sessionmaker, app.state.rawstore
    ld = {"@type": "Product", "name": "Regal", "offers": {"price": "49", "priceCurrency": "EUR"}}
    crumbs = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Produkte"},
            {"@type": "ListItem", "position": 2, "name": "Regale  "},
        ],
    }
    evidence = {
        "status": 200,
        "verdict": {"kind": "ok"},
        "snippets": {"lang": "de", "json_ld": [json.dumps(ld), json.dumps(crumbs)]},
    }
    blob = zstandard.ZstdCompressor().compress(json.dumps(evidence).encode())
    sha = hashlib.sha256(blob).hexdigest()
    store.put("snippets", sha, blob)
    async with sessionmaker() as s:
        await _import(s)
        s.add(
            RawFetch(
                id=uuid7(),
                fetched_at=datetime.now(UTC),
                url="https://shop.example/p/1",
                final_url="https://shop.example/p/1",
                status=200,
                mode="http",
                blob_sha256=sha,
                source="shop",
            )
        )
        await s.commit()
    await stage.run_until_caught_up(sm, store, settle=stage.timedelta(0))
    async with sessionmaker() as s:
        (path,) = [p["path"] for p in await shopmap.category_paths(s)]
        assert path == "Produkte > Regale"
        node = (
            await s.execute(select(TaxonomyNode.id).where(TaxonomyNode.external_id == "6356"))
        ).scalar_one()
        await shopmap.set_mapping(s, "shop", path, node)
        await s.commit()
    assert await stage.classify_until_settled(sm) == 1
    result = await stage.rebuild(sm, store, settle=stage.timedelta(0))
    assert result["identical"], result
    assert result["classified"] == 1 and result["after"]["classification"] == 1
