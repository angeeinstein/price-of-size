"""Numbers, units and dimensions from shop text."""

import pytest

from pos_shared.units import (
    axis_of,
    basis_of,
    net_or_gross,
    parse_dimensions,
    parse_number,
    parse_quantity,
)


@pytest.mark.parametrize(
    ("text", "locale", "value"),
    [
        ("1.299,00 €", "de", 1299.0),
        ("1,299.00", "en", 1299.0),
        ("€ 1.299", "de", 1299.0),  # the spec's trap: not 1.299
        ("$1,299", "en", 1299.0),
        ("1.299", "en", 1.299),  # in English this is a decimal
        ("12,5 kg", "de", 12.5),
        ("12.5 kg", "de", 12.5),
        ("1 299,90", "de", 1299.9),
        ("1’299.50", None, 1299.5),
        ("1.234.567", "de", 1234567.0),
        ("0,75", None, 0.75),
        ("7", None, 7.0),
        ("10.000,00", "de", 10000.0),
        ("44.99", "de", 44.99),  # two decimals: always decimal
        ("no number", None, None),
    ],
)
def test_parse_number(text, locale, value):
    assert parse_number(text, locale) == (pytest.approx(value) if value is not None else None)


@pytest.mark.parametrize(
    ("text", "kind", "si"),
    [
        ("Gewicht: 12,5 kg", "mass", 12.5),
        ("Nettogewicht 850 g", "mass", 0.85),
        ("Weight 3.2 lbs", "mass", 3.2 * 0.45359237),
        ("16 oz", "mass", 0.45359237),
        ("Inhalt: 1,5 l", "volume", 0.0015),
        ("330ml", "volume", 0.00033),
        ("0,5 m³", "volume", 0.5),
        ("Breite 120 cm", "length", 1.2),
        ('Bildschirm 27"', "length", 27 * 0.0254),
        ("Kabellänge 1,8m", "length", 1.8),
    ],
)
def test_parse_quantity(text, kind, si):
    q = parse_quantity(text, kind, "de")
    assert q is not None and q.value == pytest.approx(si)


def test_units_need_a_word_boundary():
    assert parse_quantity("120 mmHg", "length") is None  # not millimetres
    assert parse_quantity("5 gramm", "mass").value == pytest.approx(0.005)
    assert parse_quantity("Modell 2025 Edition", "mass") is None


@pytest.mark.parametrize(
    ("text", "dims", "labels"),
    [
        ("Maße (B×H×T): 120×80×45 cm", (1.2, 0.8, 0.45), ("B", "H", "T")),
        ("120 x 80 x 45 cm", (1.2, 0.8, 0.45), ()),
        ("45x120x80cm", (1.2, 0.8, 0.45), ()),  # sorted a >= b >= c
        ("1,2 m x 80 cm x 45 cm", (1.2, 0.8, 0.45), ()),
        ("Paketmaße: 210 x 60 x 12 mm", (0.21, 0.06, 0.012), ()),
        ("Abmessungen 30 x 20 cm", (0.3, 0.2), ()),
        ('15.6" x 10" x 1"', (15.6 * 0.0254, 0.254, 0.0254), ()),
    ],
)
def test_parse_dimensions(text, dims, labels):
    d = parse_dimensions(text, "de")
    assert d is not None
    assert d.values == pytest.approx(dims)
    assert d.labels == labels


def test_dimensions_without_unit_are_not_guessed():
    assert parse_dimensions("Größe 120 x 80") is None
    assert parse_dimensions("Version 2 x 4") is None


@pytest.mark.parametrize(
    ("label", "basis"),
    [
        ("Paketmaße", "package"),
        ("Verpackungsabmessungen", "package"),
        ("Package dimensions", "package"),
        ("Versandgewicht", "package"),
        ("Produktmaße", "product"),
        ("Maße (aufgebaut)", "product"),
        ("Abmessungen", "unknown"),
        ("Breite", "unknown"),
    ],
)
def test_basis(label, basis):
    assert basis_of(label) == basis


def test_net_gross_and_axes():
    assert net_or_gross("Bruttogewicht") == "gross"
    assert net_or_gross("Nettogewicht") == "net"
    assert net_or_gross("Gewicht") is None
    assert axis_of("Breite") == "width"
    assert axis_of("Höhe") == "height"
    assert axis_of("Tiefe") == "depth"
    assert axis_of("Länge") == "length"
    assert axis_of("Ø") == "diameter"
    assert axis_of("Farbe") is None
