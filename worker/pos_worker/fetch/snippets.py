"""Capture raw evidence from a page: everything an extractor might want later.

Collecting is slow and expensive; interpreting is cheap and repeatable. So this
module does not interpret anything: it copies the relevant parts of a page
verbatim (JSON-LD as text, tables as HTML, ...), generously but within size
caps, and the extraction stages parse them later, as often as needed.

Bump SNIPPETS_VERSION whenever what is captured changes.
"""

import json
import re
from typing import Any

from lxml import etree, html

SNIPPETS_VERSION = "2"

MAX_ITEMS = 60  # per kind
MAX_ITEM_CHARS = 200_000  # per captured element
MAX_TOTAL_CHARS = 3_000_000  # per page, all kinds together

PRICE_HINT = re.compile(r"price|preis|prix|prezzo|precio|cost|amount|betrag", re.I)
BREADCRUMB_HINT = re.compile(r"breadcrumb|brotkrume|crumb", re.I)
SPEC_HINT = re.compile(
    r"spec|technical|technisch|datenblatt|abmessung|dimension|attribute|feature|detail|"
    r"eigenschaft|merkmal|product-info|produktinfo",
    re.I,
)
META_KEEP = re.compile(r"^(og:|product:|twitter:|article:|description$|keywords$|title$)", re.I)


class _Budget:
    def __init__(self, total: int):
        self.left = total

    def take(self, text: str) -> str | None:
        text = text[:MAX_ITEM_CHARS]
        if len(text) > self.left:
            return None
        self.left -= len(text)
        return text


def _outer(el) -> str:
    return etree.tostring(el, encoding="unicode", method="html", with_tail=False)


def _text(el) -> str:
    return " ".join(el.text_content().split())


def _attr_text(el) -> str:
    return " ".join(f"{k}={v}" for k, v in el.attrib.items())


def capture(page: bytes | str, url: str | None = None) -> dict[str, Any]:
    """Return the snippets of a page as a JSON-serialisable dict."""
    budget = _Budget(MAX_TOTAL_CHARS)
    try:
        doc = html.fromstring(page)
    except (etree.ParserError, ValueError):
        return {"snippets_version": SNIPPETS_VERSION, "parse_error": True}

    out: dict[str, Any] = {"snippets_version": SNIPPETS_VERSION}
    out["lang"] = doc.get("lang") or doc.get("xml:lang")
    title = doc.find(".//title")
    out["title"] = _text(title)[:2000] if title is not None else None
    canonical = doc.xpath('//link[translate(@rel,"CANONICAL","canonical")="canonical"]/@href')
    out["canonical"] = canonical[0] if canonical else None

    meta: dict[str, list[str]] = {}
    for m in doc.xpath("//meta[@content]"):
        key = m.get("property") or m.get("name") or m.get("itemprop")
        if key and META_KEEP.search(key):
            meta.setdefault(key, []).append(m.get("content", "")[:5000])
    out["meta"] = meta

    def collect(kind: str, elements, render=_outer) -> None:
        items = []
        for el in elements:
            if len(items) >= MAX_ITEMS:
                out.setdefault("truncated", []).append(kind)
                break
            taken = budget.take(render(el))
            if taken is None:
                out.setdefault("truncated", []).append(kind)
                break
            items.append(taken)
        out[kind] = items

    # Structured data, verbatim. JSON-LD stays text: a parser bug must never lose it.
    collect(
        "json_ld",
        doc.xpath('//script[contains(translate(@type,"JSONLD","jsonld"),"ld+json")]'),
        render=lambda el: el.text or "",
    )
    out["app_state"] = _app_state(doc, budget)
    collect("microdata", _outermost(doc.xpath("//*[@itemscope][@itemtype]")))
    collect("rdfa", _outermost(doc.xpath("//*[@typeof]")))
    # Human-readable evidence
    collect(
        "breadcrumbs",
        _outermost(
            [
                el
                for el in doc.xpath("//nav|//ol|//ul|//div")
                if BREADCRUMB_HINT.search(_attr_text(el))
            ]
        ),
    )
    collect("tables", doc.xpath("//table"))
    collect("definition_lists", doc.xpath("//dl"))
    collect(
        "spec_blocks",
        _outermost(
            [
                el
                for el in doc.xpath("//section|//div|//ul")
                if SPEC_HINT.search(el.get("class", "") + " " + el.get("id", ""))
                and len(_text(el)) < 50_000
            ]
        ),
    )
    collect(
        "price_blocks",
        _innermost(
            [
                el
                for el in doc.xpath("//*[@itemprop='price' or @itemprop='offers' or @class or @id]")
                if el.get("itemprop") in ("price", "offers")
                or PRICE_HINT.search(el.get("class", "") + " " + el.get("id", ""))
            ]
        ),
    )
    collect("availability", doc.xpath("//*[@itemprop='availability']"))

    images = []
    for src in doc.xpath(
        '//meta[@property="og:image"]/@content | //*[@itemprop="image"]/@src '
        '| //*[@itemprop="image"]/@content | //img/@src'
    ):
        if src not in images:
            images.append(src[:2000])
        if len(images) >= 40:
            break
    out["image_urls"] = images

    out["has_structured_data"] = _has_product(out)
    out["chars"] = MAX_TOTAL_CHARS - budget.left
    return out


# --- embedded application state ------------------------------------------------------
# Shops that render in the browser ship their data as JSON (Next.js __NEXT_DATA__, Nuxt,
# <script type="application/json"> or "text/hydrate"): the package weight and size are
# often only there. Pages can carry megabytes of it, so only the small objects that
# describe a size are kept, each with its path ("product.packageMeasurements[0]").
STATE_SCRIPT = (
    '//script[@id="__NEXT_DATA__" or contains(translate(@type,"JSONHYDRATE","jsonhydrate"),'
    '"application/json") or contains(translate(@type,"HYDRATE","hydrate"),"hydrate")]'
)
SIZE_KEYS = {
    "weight", "netweight", "grossweight", "height", "width", "depth", "length",
    "diameter", "volume", "dimensions", "measurements", "packagemeasurements",
}  # fmt: skip
# Pixel sizes of images and videos are not product sizes: such subtrees are skipped.
MEDIA_HINT = re.compile(
    r"image|img|media|picture|photo|icon|thumb|logo|video|banner|aspect|avatar|sprite", re.I
)
MAX_STATE_SCRIPT_CHARS = 8_000_000
MAX_STATE_NODES = 40
MAX_STATE_NODE_CHARS = 3_000


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z]", "", k.lower())


def _compact(node: dict) -> dict:
    """Scalars and small {text, value, unit} objects only: what describes a size."""
    out = {}
    for k, v in node.items():
        if (
            isinstance(v, str | int | float | bool)
            or v is None
            or isinstance(v, dict)
            and all(isinstance(x, str | int | float | bool) or x is None for x in v.values())
        ):
            out[k] = v
    return out


def _state_nodes(obj, path: str, found: list, depth: int = 0) -> None:
    if depth > 40 or len(found) >= MAX_STATE_NODES:
        return
    if isinstance(obj, dict):
        keys = {_norm_key(k) for k in obj}
        if keys & {"url", "src", "imagefilename", "alt", "aspectratio", "mimetype"}:
            return  # an image or file descriptor
        if len(keys & SIZE_KEYS) >= 2 or ({"weight", "netweight", "grossweight"} & keys):
            node = _compact(obj)
            if node and len(json.dumps(node, default=str)) <= MAX_STATE_NODE_CHARS:
                found.append({"path": path, "node": node})
        for k, v in obj.items():
            if isinstance(v, dict | list) and not MEDIA_HINT.search(str(k)):
                _state_nodes(v, f"{path}.{k}" if path else str(k), found, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:500]):
            _state_nodes(v, f"{path}[{i}]", found, depth + 1)


def _app_state(doc, budget) -> list[dict]:
    found: list[dict] = []
    for el in doc.xpath(STATE_SCRIPT):
        text = el.text or ""
        if not text.strip() or len(text) > MAX_STATE_SCRIPT_CHARS:
            continue
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            continue
        _state_nodes(data, "", found)
        if len(found) >= MAX_STATE_NODES:
            break
    kept = []
    for item in found:
        if budget.take(json.dumps(item, default=str)) is None:
            break
        kept.append(item)
    return kept


def _outermost(elements):
    """Drop elements nested in another element of the same list (avoid duplicates)."""
    chosen = set(elements)
    return [el for el in elements if not any(a in chosen for a in el.iterancestors())]


def _innermost(elements):
    """Keep elements that contain no other element of the list (the specific ones)."""
    chosen = set(elements)
    return [el for el in elements if not any(d in chosen for d in el.iterdescendants())]


_PRODUCT = re.compile(r'"@type"\s*:\s*(\[[^\]]*)?"(Product|ProductGroup|Offer|IndividualProduct)"')


def _has_product(out: dict[str, Any]) -> bool:
    if any(_PRODUCT.search(j) for j in out.get("json_ld", [])):
        return True
    for el in out.get("microdata", []) + out.get("rdfa", []):
        if re.search(r"schema\.org/(Product|Offer|IndividualProduct)|typeof=\"[^\"]*Product", el):
            return True
    return False
