"""Run all extraction tiers over one fetch's raw evidence."""

from typing import Any

from pos_shared.extract import EXTRACT_VERSION, appstate, jsonld, price_rules, spectext, wikidata
from pos_shared.extract.model import Extraction


def extract_evidence(evidence: dict[str, Any]) -> Extraction:
    """`evidence` is the snippets blob a worker stored for a fetch_http job."""
    snip = evidence.get("snippets") or {}
    locale = (snip.get("lang") or "")[:2].lower() or None
    ex = Extraction(extract_version=EXTRACT_VERSION)
    pairs = jsonld.extract(snip.get("json_ld") or [], ex, locale)
    state_pairs = appstate.pairs(
        snip.get("app_state") or [], locale, {"gtin": ex.gtin, "sku": ex.sku, "mpn": ex.mpn}
    )
    if state_pairs:
        ex.sources.append("app_state")
        pairs += state_pairs
    html_pairs = spectext.pairs_from_html(
        (snip.get("tables") or [])
        + (snip.get("definition_lists") or [])
        + (snip.get("spec_blocks") or [])
    )
    if html_pairs:
        ex.sources.append("spec_table")
    ex.measurements += spectext.measurements(pairs + html_pairs, locale)
    if not ex.title:
        ex.title = snip.get("title")
    price_rules.apply(ex, snip.get("price_blocks") or [])
    verdict = (evidence.get("verdict") or {}).get("kind")
    if verdict and verdict != "ok":
        ex.flags.append(f"fetch_{verdict}")
    return ex


def api_records(evidence: dict[str, Any]) -> list[wikidata.Record]:
    """Records of an API response (`evidence["api"]`), by adapter; unknown adapters
    give none (their raw responses stay, a later version can read them)."""
    api = evidence.get("api") or {}
    if (evidence.get("status") or 0) != 200 or not isinstance(api, dict):
        return []
    if api.get("adapter") == wikidata.ADAPTER:
        params = api.get("params") if isinstance(api.get("params"), dict) else {}
        label = params.get("class_label") or params.get("class")
        return wikidata.records(api, label if isinstance(label, str) else None)
    return []
