"""The target taxonomy: Google Product Taxonomy plus the classes it lacks.

Google's list ("taxonomy-with-ids.<locale>.txt") has about 5,600 categories, one
per line as "<id> - Top > Sub > Leaf". It is imported per locale: English names
are the canonical path, other locales go to `names`. The extension adds what the
size range needs and Google does not sell: buildings and structures, land, bulk
materials, fluids, spacecraft, rail vehicles (ids "pos-...", `is_custom`).

Imports are idempotent (by taxonomy and external id): a new Google version updates
names and paths in place and adds new categories; categories it dropped stay
(classifications may point at them) and keep their old version.
"""

import re
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.models import TaxonomyNode

TAXONOMY = "google"
GOOGLE_URL = "https://www.google.com/basepages/producttype/taxonomy-with-ids.{locale}.txt"
LOCALES = {"en": "en-US", "de": "de-DE"}
SEP = " > "
CUSTOM_VERSION = "pos-1"

# (external id, English path, German name of the leaf)
CUSTOM: list[tuple[str, str, str]] = [
    ("pos-100", "Buildings & Structures", "Gebäude & Bauwerke"),
    ("pos-101", "Buildings & Structures > Buildings", "Gebäude"),
    ("pos-102", "Buildings & Structures > Bridges", "Brücken"),
    ("pos-103", "Buildings & Structures > Towers & Masts", "Türme & Masten"),
    ("pos-200", "Land", "Grundstücke"),
    ("pos-300", "Bulk Materials", "Schüttgüter & Rohstoffe"),
    ("pos-301", "Bulk Materials > Aggregates", "Kies & Sand"),
    ("pos-302", "Bulk Materials > Metals", "Metalle"),
    ("pos-303", "Bulk Materials > Cement & Concrete", "Zement & Beton"),
    ("pos-400", "Fluids", "Flüssigkeiten"),
    ("pos-401", "Fluids > Fuels", "Kraftstoffe"),
    ("pos-402", "Fluids > Water", "Wasser"),
    (
        "pos-501",
        "Vehicles & Parts > Vehicles > Spacecraft & Satellites",
        "Raumfahrzeuge & Satelliten",
    ),
    ("pos-502", "Vehicles & Parts > Vehicles > Rail Vehicles", "Schienenfahrzeuge"),
]

LINE = re.compile(r"^(\d+) - (.+)$")
VERSION = re.compile(r"^#\s*Google_Product_Taxonomy_Version:\s*(\S+)")


class TaxonomyError(ValueError):
    pass


@dataclass
class Parsed:
    version: str | None
    rows: dict[str, list[str]]  # external id -> path segments


def parse_google(text: str) -> Parsed:
    version, rows = None, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = VERSION.match(line)
            version = m.group(1) if m else version
            continue
        m = LINE.match(line)
        if not m:
            raise TaxonomyError(f"not a taxonomy line: {line[:100]!r}")
        segments = [s.strip() for s in m.group(2).split(">")]
        if any(not s for s in segments):
            raise TaxonomyError(f"empty segment: {line[:100]!r}")
        rows[m.group(1)] = segments
    if len(rows) < 100:
        raise TaxonomyError(f"only {len(rows)} categories: not a taxonomy file")
    return Parsed(version, rows)


async def import_google(
    session: AsyncSession, texts: dict[str, str], custom: bool = True
) -> dict[str, int]:
    """`texts`: locale ("en", "de") -> file content; "en" is required (the canonical
    paths). Returns counts. Parents are resolved by path."""
    if "en" not in texts:
        raise TaxonomyError("the English file is required")
    parsed = {loc: parse_google(t) for loc, t in texts.items()}
    en = parsed["en"]
    nodes: dict[str, dict] = {}
    for ext, segs in en.rows.items():
        names = {}
        for loc, p in parsed.items():
            if loc != "en" and ext in p.rows:
                names[loc] = {"name": p.rows[ext][-1], "path": SEP.join(p.rows[ext])}
        nodes[ext] = {
            "external_id": ext,
            "name": segs[-1],
            "path": SEP.join(segs),
            "depth": len(segs) - 1,
            "names": names,
            "version": en.version or "unknown",
            "is_custom": False,
        }
    existing = {
        n.external_id: n
        for n in (
            await session.execute(select(TaxonomyNode).where(TaxonomyNode.taxonomy == TAXONOMY))
        ).scalars()
    }
    if custom:
        # German paths of the parents: from this import, else as stored before
        de_paths = {n.path: (n.names or {}).get("de", {}).get("path") for n in existing.values()}
        de_paths.update(
            {n["path"]: p for n in nodes.values() if (p := n["names"].get("de", {}).get("path"))}
        )
        for ext, path, de_name in CUSTOM:
            segs = path.split(SEP)
            parent_de = de_paths.get(SEP.join(segs[:-1])) if len(segs) > 1 else None
            de_path = f"{parent_de}{SEP}{de_name}" if parent_de else de_name
            nodes[ext] = {
                "external_id": ext,
                "name": segs[-1],
                "path": path,
                "depth": len(segs) - 1,
                "names": {"de": {"name": de_name, "path": de_path}},
                "version": CUSTOM_VERSION,
                "is_custom": True,
            }
            de_paths[path] = de_path
    added = updated = 0
    # parents first, so every parent id is known when its children are written
    for node in sorted(nodes.values(), key=lambda n: n["depth"]):
        values = {**node, "taxonomy": TAXONOMY}
        stmt = pg_insert(TaxonomyNode).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["taxonomy", "external_id"],
            set_={
                **{k: stmt.excluded[k] for k in ("name", "path", "depth", "version")},
                # merged: an import without the German file keeps the German names
                "names": TaxonomyNode.names.op("||")(stmt.excluded.names),
            },
        ).returning(TaxonomyNode.id)
        node_id = (await session.execute(stmt)).scalar_one()
        if node["external_id"] in existing:
            updated += 1
        else:
            added += 1
        node["id"] = node_id
    by_path = {n["path"]: n["id"] for n in nodes.values()}
    for n in nodes.values():
        parent_path = SEP.join(n["path"].split(SEP)[:-1])
        parent = by_path.get(parent_path) if parent_path else None
        if parent_path and parent is None:
            raise TaxonomyError(f"no parent for {n['path']!r}")
        await session.execute(
            TaxonomyNode.__table__.update()
            .where(TaxonomyNode.id == n["id"])
            .values(parent_id=parent)
        )
    return {"added": added, "updated": updated, "total": len(nodes), "version": en.version}


async def download(locales=("en", "de"), client: httpx.AsyncClient | None = None) -> dict[str, str]:
    """The Google files for `locales` (the coordinator fetches them, like ECB rates)."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        out = {}
        for loc in locales:
            r = await client.get(GOOGLE_URL.format(locale=LOCALES[loc]))
            r.raise_for_status()
            out[loc] = r.text
        return out
    finally:
        if own:
            await client.aclose()
