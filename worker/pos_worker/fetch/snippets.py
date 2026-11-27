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

from pos_shared.extract.appstate import is_box_list_segment, is_package_segment

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


MEDIA_KEYS = {"src", "srcset", "imagefilename", "mimetype", "aspectratio", "alt", "alttext"}
MEDIA_FILE = re.compile(r"\.(jpe?g|png|webp|avif|gif|svg|tiff?|mp4|webm|mov)(\?|#|$)", re.I)


def _is_media(obj: dict, keys: set[str]) -> bool:
    """An image or video descriptor, not a product: media keys, or a URL to a media file.
    A product object with its own page URL is not one (its sizes must not be lost)."""
    if keys & MEDIA_KEYS:
        return True
    url = next((v for k, v in obj.items() if _norm_key(k) in ("url", "href")), None)
    return isinstance(url, str) and bool(MEDIA_FILE.search(url))


# Keys that identify a product: the nearest object with one owns the sizes below it,
# so extraction can tell the page's product from recommended or related ones.
ID_KEYS = {
    "sku", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "ean", "mpn", "productid",
    "productnumber", "itemno", "itemnumber", "articlenumber", "articleno", "artnr",
}  # fmt: skip


def _owner_ids(obj: dict) -> dict[str, str]:
    ids = {}
    for k, v in obj.items():
        nk = _norm_key(k)
        if nk in ID_KEYS | {"id", "name", "title"} and isinstance(v, str | int) and v != "":
            ids[nk] = str(v)[:120]
    if ids.keys() & ID_KEYS or ("id" in ids and ids.keys() & {"name", "title"}):
        return ids
    return {}


def _inside_package(path: str, owner) -> bool:
    """A box with its own item number belongs to the product around it, not to itself
    (a product with SKU A shipped in two numbered boxes is one product)."""
    below = path[len(owner["path"]) :] if owner else path
    return any(is_package_segment(seg) for seg in re.split(r"[.\[\]]+", below) if seg)


def _box_entry(path: str) -> bool:
    """An entry of a list of boxes ("product.packages[2]")."""
    m = re.search(r"([^.\[\]]+)\[\d+\]$", path)
    return bool(m and is_box_list_segment(m.group(1)))


def _state_nodes(obj, path: str, found: list, meta: dict, depth: int = 0, owner=None) -> None:
    """Collect size nodes into `found`; `meta["truncated"]` when one had to be left out
    (limits): then the capture is incomplete and package totals must not be summed."""
    if meta["truncated"]:
        return
    if depth > 40:
        meta["truncated"] = True
        return
    if isinstance(obj, dict):
        keys = {_norm_key(k) for k in obj}
        if _is_media(obj, keys):
            return  # an image or file descriptor: its width and height are pixels
        ids = _owner_ids(obj)
        if ids and not _inside_package(path, owner):
            owner = {"path": path, "ids": ids}
        sized = len(keys & SIZE_KEYS)
        box_entry = _box_entry(path)
        if (
            sized >= 2
            or {"weight", "netweight", "grossweight"} & keys
            or (sized and _inside_package(path, None))
            or box_entry
        ):
            # Inside a package every box counts, also one with only "dimensions" or no
            # size at all: a box without a known weight must stop a total, not vanish.
            node = _compact(obj)
            if not node and not box_entry:
                pass
            elif len(found) >= MAX_STATE_NODES or (
                len(json.dumps(node, default=str)) > MAX_STATE_NODE_CHARS
            ):
                meta["truncated"] = True
                return
            else:
                found.append({"path": path, "node": node, **({"owner": owner} if owner else {})})
        for k, v in obj.items():
            if isinstance(v, dict | list) and not MEDIA_HINT.search(str(k)):
                _state_nodes(v, f"{path}.{k}" if path else str(k), found, meta, depth + 1, owner)
    elif isinstance(obj, list):
        # No cap on the length: the script itself is bounded (MAX_STATE_SCRIPT_CHARS).
        for i, v in enumerate(obj):
            _state_nodes(v, f"{path}[{i}]", found, meta, depth + 1, owner)


def _app_state(doc, budget) -> list[dict]:
    """Size nodes of the page's state; a last {"truncated": true} entry if limits cut
    the capture short."""
    found: list[dict] = []
    meta = {"truncated": False}
    for el in doc.xpath(STATE_SCRIPT):
        text = el.text or ""
        if not text.strip() or len(text) > MAX_STATE_SCRIPT_CHARS:
            continue
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            continue
        _state_nodes(data, "", found, meta)
        if meta["truncated"]:
            break
    kept = []
    for item in found:
        if budget.take(json.dumps(item, default=str)) is None:
            meta["truncated"] = True
            break
        kept.append(item)
    if meta["truncated"]:
        kept.append({"truncated": True})
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
