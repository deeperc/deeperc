"""TODO-554 — explicit part-string -> cache-stem bindings (steps/part_bindings.py).

TODO-225 retired the one real entry (BINDINGS is {}); the mechanism tests are
driven by a monkeypatched synthetic entry. Every test isolates both cache roots into tmp_path; the real trees are never
read or written.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline  # noqa: E402
import provenance  # noqa: E402
from steps import part_bindings  # noqa: E402
from steps import step_03_resolver as r3  # noqa: E402

TARGET = "Widget-X(PKG)"
BOUND = "widget-x-real"
SIBLING = "widget-x"


@pytest.fixture(autouse=True)
def _synthetic_binding(monkeypatch):
    monkeypatch.setitem(part_bindings.BINDINGS, TARGET, BOUND)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Isolated canonical + staging roots, staging reads OFF (the default)."""
    canonical = tmp_path / "parsed"
    staging = tmp_path / "staged"
    datasheets = tmp_path / "datasheets"
    canonical.mkdir()
    staging.mkdir()
    datasheets.mkdir()
    monkeypatch.setattr(r3, "PARSED_DIR", str(canonical))
    monkeypatch.setattr(r3, "STAGING_DIR", str(staging))
    monkeypatch.setattr(r3, "STAGING_ENABLED", False)
    monkeypatch.setattr(r3, "DATASHEETS_DIR", str(datasheets))
    monkeypatch.setattr(r3, "_manifest_state",
                        {"dir": None, "data": None, "notice_logged": False})
    return {"canonical": canonical, "staging": staging, "datasheets": datasheets}


def _mkdirs(root, *names):
    for n in names:
        (root / n).mkdir(parents=True, exist_ok=True)


def _name(hit):
    return os.path.basename(str(hit)) if hit else None


def _write_cache(parsed_dir, stem):
    d = parsed_dir / stem / "auto"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}_pin_groups.json").write_text(json.dumps({
        "pin_groups": [{"group_name": "Supply (VCC)", "pin_type": "power",
                        "example_pins": ["VCC"]}],
        "default_group": "Supply (VCC)",
        "extraction_meta": {"extractor": "claude-code", "source": "foo.md",
                            "schema_version": 1,
                            "r1_correction": {"date": "2026-08-14"}},
        "provenance": {"source_hash": "deadbeef", "parse_config": {}, "pdf_size": 1},
    }, indent=2))


# ── bound_stem ──────────────────────────────────────────────────────────────

def test_bound_stem_exact():
    assert part_bindings.bound_stem(TARGET) == BOUND
    assert part_bindings.bound_stem(f"  {TARGET}\n") == BOUND


def test_shipped_binding_table(monkeypatch):
    monkeypatch.undo()  # drop the synthetic entry; read the real table
    # TODO-556 decision 20: the one shipped entry (was empty after TODO-225).
    assert part_bindings.BINDINGS == {"ESP32-WROOM-32D-4MB": "esp32-wroom-32d"}


@pytest.mark.parametrize("probe", ["widget-x(pkg)", "Widget-X", "Widget"])
def test_bound_stem_is_case_sensitive_and_exact(probe):
    assert part_bindings.bound_stem(probe) is None


def test_every_binding_key_has_an_uppercase_letter():
    # The export collision tools probe with lower-case cache-stem names; an
    # upper-case key can never be hit by them. Guards future entries.
    for key in part_bindings.BINDINGS:
        assert any(c.isupper() for c in key), key


# ── find_cached_dir_for_part ────────────────────────────────────────────────

def test_bound_part_resolves_to_its_stem_despite_sibling(roots):
    _mkdirs(roots["canonical"], SIBLING, BOUND)
    assert _name(r3.find_cached_dir_for_part(TARGET)) == BOUND


def test_unbound_stripped_form_stays_ambiguous(roots):
    _mkdirs(roots["canonical"], SIBLING, BOUND)
    assert r3.find_cached_dir_for_part("Widget-X") is None


def test_bound_stem_absent_is_a_miss(roots):
    _mkdirs(roots["canonical"], SIBLING)
    assert r3.find_cached_dir_for_part(TARGET) is None


def test_staging_off_ignores_staged_bound_dir(roots):
    _mkdirs(roots["staging"], BOUND)
    assert r3.find_cached_dir_for_part(TARGET) is None


def test_staging_on_finds_staged_bound_dir(roots, monkeypatch):
    _mkdirs(roots["staging"], BOUND)
    monkeypatch.setattr(r3, "STAGING_ENABLED", True)
    hit = r3.find_cached_dir_for_part(TARGET)
    assert hit == roots["staging"] / BOUND


def test_canonical_wins_over_staged(roots, monkeypatch):
    _mkdirs(roots["canonical"], BOUND)
    _mkdirs(roots["staging"], BOUND)
    monkeypatch.setattr(r3, "STAGING_ENABLED", True)
    assert r3.find_cached_dir_for_part(TARGET) == roots["canonical"] / BOUND


def test_non_bound_part_unchanged(roots):
    _mkdirs(roots["canonical"], "esp32-wroom-32e-n4", "esp32-wroom-32e-n16")
    assert _name(r3.find_cached_dir_for_part("ESP32-WROOM-32E-N4")) == "esp32-wroom-32e-n4"


# ── warm shortcut end to end ────────────────────────────────────────────────

def test_warm_shortcut_serves_bound_part_without_a_pdf(roots):
    _mkdirs(roots["canonical"], SIBLING)  # the empty sibling
    _write_cache(roots["canonical"], BOUND)
    doc = pipeline._try_warm_cache_shortcut(TARGET, frozenset())
    assert doc is not None
    assert doc["pdf_stem"] == BOUND
    assert doc["pdf_path"] is None
    assert doc["resolver"] == "cache_warm_no_pdf"


# ── era membership ──────────────────────────────────────────────────────────

def test_part_bindings_is_an_era_member():
    assert "steps/part_bindings.py" in provenance.CORE_PIPELINE_FILES
    assert "steps/part_bindings.py" in provenance.CHECKER_SOURCE_FILES
