"""Run all extraction tiers over one fetch's raw evidence."""

from typing import Any

from pos_shared.extract import EXTRACT_VERSION, appstate, jsonld, price_rules, spectext
from pos_shared.extract.model import Extraction


def extract_evidence(evidence: dict[str, Any]) -> Extraction:
    """`evidence` is the snippets blob a worker stored for a fetch_http job."""
    snip = evidence.get("snippets") or {}
    locale = (snip.get("lang") or "")[:2].lower() or None
    ex = Extraction(extract_version=EXTRACT_VERSION)
    pairs = jsonld.extract(snip.get("json_ld") or [], ex, locale)
    state_pairs = appstate.pairs(snip.get("app_state") or [])
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
