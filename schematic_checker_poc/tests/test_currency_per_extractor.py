"""Per-extractor cache-currency tests (TODO-397).

investigation/recon_reports/todo397_398_currency_label_recon.md found that
haiku_pdfplumber (extractors.py DEFAULT_EXTRACTOR) never reads any of
parse_config()'s four keys — comparing parse_config against a haiku-produced
cache false-flags "stale" on a byte-identical PDF whenever an unrelated
MinerU-only setting (mineru_version, install-dependent) differs, which is
exactly what a MinerU-absent stranger install hits. These tests pin the
per-extractor currency split in check_cache_provenance: haiku_pdfplumber's
currency is PDF identity alone (source_pdf_sha256); gemma_mineru and the
no-extractor_impl legacy case are byte-for-byte unchanged.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import step_03_resolver as r3  # noqa: E402


@pytest.fixture
def tmp_pdf(tmp_path):
    p = tmp_path / "FOO123.pdf"
    p.write_bytes(b"%PDF-1.4 fake content for hashing\n" * 100)
    return str(p)


@pytest.fixture
def tmp_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(r3, "PARSED_DIR", str(tmp_path / "parsed"))
    monkeypatch.setattr(r3, "STAGING_DIR", str(tmp_path / "parsed"))
    monkeypatch.setattr(r3, "QUARANTINE_DIR", str(tmp_path / "quarantine"))
    return tmp_path / "parsed"


def test_haiku_cache_current_despite_mineru_version_mismatch(tmp_pdf, tmp_parsed, monkeypatch):
    """The stranger-install scenario: mineru_version differs (installed
    magic-pdf vs "unknown" on a MinerU-absent venv) but the PDF is
    byte-identical. haiku_pdfplumber must read this as current — the whole
    point of TODO-397."""
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="haiku",
                              extractor_impl="haiku_pdfplumber")
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    assert cached["provenance"]["extractor_impl"] == "haiku_pdfplumber"
    monkeypatch.setattr(r3, "MINERU_VERSION", "unknown")
    assert r3.check_cache_provenance(tmp_pdf, cached) == "current"


def test_haiku_cache_stale_on_sha_mismatch(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="haiku",
                              extractor_impl="haiku_pdfplumber")
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    with open(tmp_pdf, "ab") as f:
        f.write(b"MUTATED")
    assert r3.check_cache_provenance(tmp_pdf, cached) == "stale"


def test_haiku_cache_stale_on_pdf_size_shortcut(tmp_pdf, tmp_parsed):
    """The pdf_size cost-saver short-circuit: a size mismatch alone is
    sufficient to return stale without computing the sha256, and must still
    reach the same "stale" verdict as an explicit hash mismatch."""
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="haiku",
                              extractor_impl="haiku_pdfplumber")
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    with open(tmp_pdf, "ab") as f:
        f.write(b"MORE BYTES CHANGING THE SIZE")
    assert r3.check_cache_provenance(tmp_pdf, cached) == "stale"


def test_gemma_mineru_cache_still_stales_on_mineru_version_mismatch(tmp_pdf, tmp_parsed, monkeypatch):
    """Regression guard: the gemma_mineru path is untouched — a
    mineru_version drift must still surface as stale for this extractor,
    unlike haiku_pdfplumber."""
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma",
                              extractor_impl="gemma_mineru")
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    assert cached["provenance"]["extractor_impl"] == "gemma_mineru"
    monkeypatch.setattr(r3, "MINERU_VERSION", "some-other-version")
    assert r3.check_cache_provenance(tmp_pdf, cached) == "stale"


def test_no_extractor_impl_behaves_exactly_as_today(tmp_pdf, tmp_parsed, monkeypatch):
    """A cache with no extractor_impl field (pre-interface / legacy) must
    fall through to the unchanged shared parse_config path — same behavior
    as test_provenance_detects_config_change in test_cache_provenance.py."""
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    assert "extractor_impl" not in cached["provenance"]
    assert r3.check_cache_provenance(tmp_pdf, cached) == "current"
    monkeypatch.setattr(r3, "TARGET_PHRASES", r3.TARGET_PHRASES + ["recommended operating"])
    assert r3.check_cache_provenance(tmp_pdf, cached) == "stale"
