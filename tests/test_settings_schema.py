"""The panel's settings form and the server's checks agree on every setting."""

import pytest

from pos_coordinator import runtime_settings, settings_schema

FIELDS = [f for g in settings_schema.GROUPS for f in g["fields"]]
VALID = runtime_settings.VALIDATORS


def accepts(key, value) -> bool:
    try:
        return bool(VALID[key](value))
    except runtime_settings.InvalidSetting:
        return False


def test_every_setting_is_shown_once_or_listed_as_unused():
    shown = [f["key"] for f in FIELDS]
    assert len(shown) == len(set(shown))
    assert not set(shown) & set(settings_schema.UNUSED)
    assert set(shown) | set(settings_schema.UNUSED) == set(runtime_settings.DEFAULTS)


@pytest.mark.parametrize("f", FIELDS, ids=lambda f: f["key"])
def test_the_form_matches_the_checks(f):
    key, kind = f["key"], f["kind"]
    assert accepts(key, runtime_settings.DEFAULTS[key])
    assert accepts(key, None) == bool(f.get("nullable")) or kind == "managed"
    if kind == "bool":
        assert accepts(key, False) and accepts(key, True) and not accepts(key, "yes")
    elif kind in ("int", "number"):
        lo, hi = f["min"], f.get("max")
        assert accepts(key, lo)
        below = lo - (1 if kind == "int" else 0.001)
        assert not accepts(key, below)
        if hi is not None:
            assert accepts(key, hi) and not accepts(key, hi + 1)
        assert not accepts(key, "5") and not accepts(key, True)
        if kind == "int":
            assert not accepts(key, lo + 0.5)
    elif kind == "percent":
        assert accepts(key, 0) and accepts(key, 1) and accepts(key, 0.25)
        assert not accepts(key, 1.01) and not accepts(key, -0.01)
    elif kind == "enum":
        assert all(accepts(key, o["value"]) for o in f["options"])
        assert not accepts(key, "bogus")
    elif kind == "string":
        assert accepts(key, "x" * f["min_length"]) and accepts(key, "x" * f["max_length"])
        assert not accepts(key, "x" * (f["min_length"] - 1))
        assert not accepts(key, "x" * (f["max_length"] + 1))
    elif kind == "models":
        roles = [r["role"] for r in f["roles"]]
        assert "default" in roles and set(roles) <= set(runtime_settings.DEFAULTS[key])
        models = [m["id"] for m in settings_schema.schema()["models"]]
        assert accepts(key, dict(zip(roles, models, strict=False)))
    elif kind == "generation":
        assert accepts(key, 0) and accepts(key, 7) and not accepts(key, -1)
    else:
        assert kind == "managed", kind


async def test_the_panel_gets_the_schema(admin):
    r = await admin.get("/api/admin/settings/schema")
    assert r.status_code == 200
    body = r.json()
    assert body["groups"][0]["fields"][0]["key"] == "respect_robots_txt"
    haiku = next(m for m in body["models"] if m["id"] == "claude-haiku-5-5")
    assert haiku["label"] == "Haiku 5.5" and haiku["output_usd"] > haiku["input_usd"] > 0
    assert body["defaults"]["base_rate_rps"] == 0.2
