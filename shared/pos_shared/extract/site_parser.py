"""Tier 2 of extraction: per-site declarative parsers (docs/spec.md, "Extraction").

A site parser is data, not code: CSS selectors (with an optional attribute and an
optional regular expression) that say where a site's pages show the title, the
price, the breadcrumbs and the size lines. This module is the trusted engine that
runs such a spec over an archived page. Specs are written by the LLM (and checked
on held-out pages before they are activated), so they are treated as untrusted:
strict schema, bounded sizes, selectors compiled before use, regular expressions
short and without nested repetition, and inputs to them cut short.

Like the LLM tier, a spec does not compute values: it points at text, and the
usual parsers (numbers, units, size labels) read it.
"""

import functools
import re
from dataclasses import dataclass, field
from typing import Any

from cssselect import SelectorError
from lxml import etree, html
from lxml.cssselect import CSSSelector
from pydantic import BaseModel, ConfigDict

from pos_shared import units
from pos_shared.extract import spectext
from pos_shared.extract.model import Extraction, OfferCandidate

ENGINE_VERSION = "1"
MAX_SELECTOR = 300
MAX_REGEX = 200
MAX_TEXT = 500  # what a regular expression sees of an element's text
MAX_ROW_RULES = 5
MAX_ROWS = 80
CONFIDENCE = 0.8
METHOD = "site_parser"
COMMON_CURRENCIES = {"EUR", "USD", "GBP", "CHF", "PLN", "CZK", "SEK", "DKK", "NOK", "HUF", "JPY",
                     "CNY", "CAD", "AUD"}  # fmt: skip

# --- the spec ------------------------------------------------------------------------


class Pick(BaseModel):
    """One value: the `index`-th element matching `css`, its text (or attribute
    `attr`), optionally narrowed by `regex` (its first group, else the match)."""

    model_config = ConfigDict(extra="forbid")
    css: str
    attr: str | None
    regex: str | None
    index: int


class Rows(BaseModel):
    """Label and value pairs: each element matching `css` is a row, `label` and
    `value` are selectors inside it (a spec table, a definition list)."""

    model_config = ConfigDict(extra="forbid")
    css: str
    label: str
    value: str


class Labelled(BaseModel):
    """A size line whose label is not on the page as such ("Gewicht": a pick)."""

    model_config = ConfigDict(extra="forbid")
    label: str
    pick: Pick


class SiteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Pick | None
    brand: Pick | None
    gtin: Pick | None
    price: Pick | None
    currency: Pick | None
    currency_default: str | None  # ISO 4217 when the page never names it
    availability: Pick | None
    category: Pick | None  # every match is one breadcrumb level (index ignored)
    size_rows: list[Rows]
    size_picks: list[Labelled]


class SpecError(ValueError):
    pass


_NESTED_REPEAT = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{?]")


def _check_regex(rx: str) -> None:
    if len(rx) > MAX_REGEX:
        raise SpecError("regular expression too long")
    if _NESTED_REPEAT.search(rx):
        raise SpecError("nested repetition in a regular expression")
    try:
        re.compile(rx)
    except re.error as e:
        raise SpecError(f"bad regular expression: {e}") from e


def _check_css(sel: str) -> None:
    if not sel.strip() or len(sel) > MAX_SELECTOR:
        raise SpecError("selector empty or too long")
    try:
        _selector(sel)
    except (SelectorError, etree.XPathError, ValueError) as e:
        raise SpecError(f"bad selector {sel!r}: {e}") from e


def _check_pick(p: Pick | None) -> None:
    if p is None:
        return
    _check_css(p.css)
    if p.attr is not None and not re.fullmatch(r"[A-Za-z_:][-A-Za-z0-9_:.]{0,49}", p.attr):
        raise SpecError("bad attribute name")
    if p.regex is not None:
        _check_regex(p.regex)
    if not 0 <= p.index <= 50:
        raise SpecError("index out of range")


def check(spec: SiteSpec) -> SiteSpec:
    """Raise SpecError unless the spec is safe and well-formed."""
    for p in (spec.title, spec.brand, spec.gtin, spec.price, spec.currency,
              spec.availability, spec.category):  # fmt: skip
        _check_pick(p)
    if len(spec.size_rows) > MAX_ROW_RULES or len(spec.size_picks) > 20:
        raise SpecError("too many size rules")
    for r in spec.size_rows:
        for sel in (r.css, r.label, r.value):
            _check_css(sel)
    for lp in spec.size_picks:
        if not lp.label.strip() or len(lp.label) > 100:
            raise SpecError("bad size label")
        _check_pick(lp.pick)
    if spec.currency_default is not None and not re.fullmatch(r"[A-Z]{3}", spec.currency_default):
        raise SpecError("currency_default must be an ISO 4217 code")
    if not (spec.price or spec.size_rows or spec.size_picks):
        raise SpecError("the spec extracts neither a price nor sizes")
    return spec


def parse(artifact: dict[str, Any]) -> SiteSpec:
    """A stored artifact as a checked spec (keys starting with "_" are notes, such as
    the validation result, and are not part of the spec)."""
    try:
        spec = SiteSpec.model_validate(
            {k: v for k, v in (artifact or {}).items() if not k.startswith("_")}
        )
    except ValueError as e:
        raise SpecError(str(e)[:500]) from e
    return check(spec)


# --- the engine ----------------------------------------------------------------------


@functools.lru_cache(maxsize=2048)
def _selector(css: str) -> CSSSelector:
    return CSSSelector(css)


def _text(el: Any) -> str:
    if not isinstance(el, etree._Element):
        return " ".join(str(el).split())
    return " ".join(el.text_content().split())[:2000]


def _value(el: Any, pick: Pick) -> str | None:
    raw = el.get(pick.attr) if pick.attr else _text(el)
    if raw is None:
        return None
    raw = " ".join(str(raw).split())
    if pick.regex:
        m = re.search(pick.regex, raw[:MAX_TEXT])
        if not m:
            return None
        raw = m.group(1) if m.groups() and m.group(1) is not None else m.group(0)
    return raw.strip()[:500] or None


def _one(doc: Any, pick: Pick | None) -> str | None:
    if pick is None:
        return None
    found = _selector(pick.css)(doc)
    if len(found) <= pick.index:
        return None
    return _value(found[pick.index], pick)


@dataclass
class SiteResult:
    title: str | None = None
    brand: str | None = None
    gtin: str | None = None
    price_text: str | None = None
    currency: str | None = None
    availability: str | None = None
    category_path: list[str] = field(default_factory=list)
    pairs: list[tuple[str, str]] = field(default_factory=list)


_AVAILABILITY = (
    ("outofstock", "out_of_stock"),
    ("soldout", "out_of_stock"),
    ("discontinued", "discontinued"),
    ("preorder", "preorder"),
    ("instock", "in_stock"),
)


def _availability(v: str | None) -> str | None:
    key = re.sub(r"[^a-z]", "", (v or "").lower().rsplit("/", 1)[-1])
    return next((out for k, out in _AVAILABILITY if k in key), None)


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]{1,40})""", re.I)


def decode(page: bytes | str) -> str:
    """An archived page as text: the charset its meta tag declares, else UTF-8, else
    Windows-1252 (what browsers fall back to for Western pages)."""
    if isinstance(page, str):
        return page
    m = _META_CHARSET.search(page[:4096])
    for enc in ((m.group(1).decode("ascii", "ignore") if m else None), "utf-8"):
        if not enc:
            continue
        try:
            return page.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return page.decode("cp1252", errors="replace")


def _doc(page: bytes | str) -> Any:
    text = decode(page)
    if not text.strip():
        return None
    try:
        return html.fromstring(text)
    except (etree.ParserError, ValueError):
        return None


def run(spec: SiteSpec, page: bytes | str) -> SiteResult:
    doc = _doc(page)
    if doc is None:
        return SiteResult()
    out = SiteResult(
        title=_one(doc, spec.title),
        brand=_one(doc, spec.brand),
        gtin=_one(doc, spec.gtin),
        price_text=_one(doc, spec.price),
        currency=_one(doc, spec.currency),
        availability=_availability(_one(doc, spec.availability)),
    )
    if spec.category is not None:
        crumbs = [_value(el, spec.category) for el in _selector(spec.category.css)(doc)[:10]]
        out.category_path = [c[:100] for c in crumbs if c]
    for rule in spec.size_rows:
        for row in _selector(rule.css)(doc)[:MAX_ROWS]:
            label = next((_text(e) for e in _selector(rule.label)(row)), "")
            value = next((_text(e) for e in _selector(rule.value)(row)), "")
            if label and value and len(out.pairs) < MAX_ROWS:
                out.pairs.append((label[:100], value[:200]))
    for lp in spec.size_picks:
        v = _one(doc, lp.pick)
        if v:
            out.pairs.append((lp.label, v[:200]))
    return out


def currency_of(result: SiteResult, spec: SiteSpec) -> str | None:
    """The ISO code of the price: named on the page, as a symbol, or the default."""
    named = re.fullmatch(r"\s*([A-Z]{3})\s*", result.currency or "")
    if named:
        return named.group(1)  # an unknown code is flagged later (no exchange rate)
    text = (result.currency or "") + " " + (result.price_text or "")
    m = re.search(r"\b([A-Z]{3})\b", text)
    if m and m.group(1) in COMMON_CURRENCIES:
        return m.group(1)
    for symbol, code in (("€", "EUR"), ("£", "GBP"), ("CHF", "CHF"), ("$", "USD")):
        if symbol in text:
            return code
    return spec.currency_default


def apply(
    ex: Extraction, result: SiteResult, spec: SiteSpec, locale: str | None, version: str
) -> bool:
    """Fill the gaps of `ex` (as the LLM tier does) from a site parser's result."""
    from pos_shared.extract import llm_page

    need = llm_page.missing(ex) if ex.title else ["price", "size"]
    added = False
    if not ex.title and result.title:
        ex.title = result.title[:300]
    if "price" in need and result.price_text:
        price = units.parse_number(result.price_text, locale)
        if price is not None:
            ex.offers.append(
                OfferCandidate(
                    price=price,
                    currency=currency_of(result, spec),
                    price_raw=result.price_text[:100],
                    availability=result.availability,
                    method=f"{METHOD}:{version}",
                    confidence=CONFIDENCE,
                )
            )
            added = True
    if "size" in need and result.pairs:
        found = spectext.measurements(result.pairs[:MAX_ROWS], locale)
        for m in found:
            m.method, m.confidence = f"{METHOD}:{version}", CONFIDENCE
        ex.measurements += found
        added = added or bool(found)
    if not ex.category_path and result.category_path:
        ex.category_path = result.category_path[:8]
    # (no brand or GTIN from a site parser: they decide the product's match key, and a
    # product keeps the identity its first derivation gave it; the parser's values
    # stay visible in its test runs)
    if added:
        ex.sources.append(METHOD)
    return added


# --- what the LLM sees of a page when it writes a spec -------------------------------

_DROP = {"script", "style", "noscript", "svg", "iframe", "template", "canvas", "video", "audio"}
_KEEP_ATTRS = {"id", "class", "itemprop", "itemtype", "content", "href", "datetime", "title"}


def condensed_html(page: bytes | str, limit: int = 20_000) -> str:
    """The page's body as compact HTML for the LLM: no scripts, styles or media,
    only the attributes selectors use, whitespace collapsed; deterministic."""
    doc = _doc(page)
    if doc is None:
        return ""
    for el in list(doc.iter()):
        if not isinstance(el.tag, str):  # comments, processing instructions
            if el.getparent() is not None:
                el.getparent().remove(el)
            continue
        if el.tag in _DROP:
            if el.getparent() is not None:
                el.drop_tree()
            continue
        for a in list(el.attrib):
            if a not in _KEEP_ATTRS and not a.startswith("data-"):
                del el.attrib[a]
            elif len(el.attrib[a]) > 120:
                el.attrib[a] = el.attrib[a][:120]
    body = doc.find("body")
    root = body if body is not None else doc
    text = etree.tostring(root, encoding="unicode", method="html")
    return re.sub(r"\s+", " ", text)[:limit]
