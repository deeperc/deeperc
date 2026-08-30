"""Unit tests for cache provenance (Commit A) + the quarantine guard predicate
(Commit B). No network. Uses a tmp PDF + tmp PARSED_DIR; never touches the real
datasheets_parsed cache.
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
    """Isolated cache root for the provenance round-trips below.

    TODO-386 Phase 3: extraction writes now default to STAGING_DIR (R-C), so
    both roots are pointed at the SAME tmp directory here. That keeps every
    test in this module testing what it was written to test — provenance
    stamping, the quarantine guard, the backfill — rather than tier routing,
    which is covered on its own in tests/test_staging_tier.py.
    """
    monkeypatch.setattr(r3, "PARSED_DIR", str(tmp_path / "parsed"))
    monkeypatch.setattr(r3, "STAGING_DIR", str(tmp_path / "parsed"))
    # TODO-388 Phase 2 (R-β): third root, isolated for the same reason.
    monkeypatch.setattr(r3, "QUARANTINE_DIR", str(tmp_path / "quarantine"))
    return tmp_path / "parsed"


def test_save_writes_provenance_block(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    prov = cached["provenance"]
    assert prov["source_hash"] and prov["source_pdf_sha256"]
    # TODO-227 Phase 3a: max_pdfplumber_pages retired from parse_config() —
    # the page-count cap is gone, replaced by a per-document RSS ceiling that
    # lives in guard_config, not parse_config (see _guard_config).
    assert "max_pdfplumber_pages" not in prov["parse_config"]
    assert "target_phrases" in prov["parse_config"]
    assert prov["parse_quality"] in ("current_code_full", "full_precap")


def test_provenance_roundtrip_current(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    assert r3.check_cache_provenance(tmp_pdf, cached) == "current"


def test_provenance_detects_config_change(tmp_pdf, tmp_parsed, monkeypatch):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    # a TARGET_PHRASES change must surface as stale (the M2-broadening scenario)
    monkeypatch.setattr(r3, "TARGET_PHRASES", r3.TARGET_PHRASES + ["recommended operating"])
    assert r3.check_cache_provenance(tmp_pdf, cached) == "stale"


def test_provenance_detects_pdf_change(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    with open(tmp_pdf, "ab") as f:
        f.write(b"MUTATED")
    assert r3.check_cache_provenance(tmp_pdf, cached) == "stale"


def test_legacy_cache_unverified(tmp_pdf):
    assert r3.check_cache_provenance(tmp_pdf, {"pin_groups": [], "extraction_meta": {}}) == "legacy_unverified"


def test_would_degrade_guard(tmp_pdf, tmp_parsed, monkeypatch):
    # full_precap cache on a >cap PDF (current code skips MinerU) must be guarded;
    # current-code cache is not; a <=cap PDF is MinerU-reproducible so not a degrade.
    monkeypatch.setattr(r3, "MAX_PDF_SIZE_FOR_MINERU", 1)  # tmp_pdf bytes > 1 => would degrade
    assert r3.would_degrade_parse(tmp_pdf, {"provenance": {"parse_quality": "full_precap"}}) is True
    assert r3.would_degrade_parse(tmp_pdf, {"provenance": {"parse_quality": "current_code_full"}}) is False
    monkeypatch.setattr(r3, "MAX_PDF_SIZE_FOR_MINERU", 10 ** 9)  # tmp_pdf <= cap => reproducible
    assert r3.would_degrade_parse(tmp_pdf, {"provenance": {"parse_quality": "full_precap"}}) is False


def test_save_refuses_to_overwrite_full_precap(tmp_pdf, tmp_parsed, monkeypatch):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": [{"group_name": "RICH"}]},
                             extractor="manual", parse_quality="full_precap")
    monkeypatch.setattr(r3, "MAX_PDF_SIZE_FOR_MINERU", 1)  # current code would degrade
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")  # default re-extract
    kept = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    assert kept["pin_groups"] == [{"group_name": "RICH"}]  # NOT overwritten
    assert kept["provenance"]["parse_quality"] == "full_precap"


# ── self-healing provenance backfill (persist; Notion 3933272c-88f3-81fe) ─────

def _write_legacy_cache(tmp_pdf, pin_groups, extraction_meta=None):
    cp = r3.get_pin_groups_cache_path(tmp_pdf)
    cp.parent.mkdir(parents=True, exist_ok=True)
    d = {"pin_groups": pin_groups}
    if extraction_meta:
        d["extraction_meta"] = extraction_meta
    cp.write_text(json.dumps(d, indent=2))
    return cp


def test_backfill_persists_and_pin_groups_identical(tmp_pdf, tmp_parsed):
    groups = [{"pin_type": "power", "supply_rail_name": "VCC", "supply_min": 3.0, "supply_max": 3.6}]
    meta = {"extractor": "gemma", "model": "x", "source": "FOO123.md"}
    cp = _write_legacy_cache(tmp_pdf, groups, meta)
    out = r3.persist_provenance_if_missing(tmp_pdf, json.loads(cp.read_text()))
    assert out == "stamped"
    after = json.loads(cp.read_text())
    assert after["provenance"]["source_hash"]                 # provenance persisted
    assert after["pin_groups"] == groups                      # content byte-identical
    assert after["extraction_meta"]["extractor"] == "gemma"   # real extractor preserved
    assert cp.with_name(cp.name + ".prebackfill").exists()    # copy-before-write


def test_backfill_noop_when_already_stamped(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")  # has provenance
    cp = r3.get_pin_groups_cache_path(tmp_pdf)
    before = cp.read_text()
    assert r3.persist_provenance_if_missing(tmp_pdf, json.loads(before)) == "already"
    assert cp.read_text() == before                           # untouched, not rewritten
    assert not cp.with_name(cp.name + ".prebackfill").exists()


def test_backfill_no_pdf_stays_legacy(tmp_parsed, tmp_path):
    missing = str(tmp_path / "GONE.pdf")                       # no file → _pdf_sha256 None
    assert r3.persist_provenance_if_missing(missing, {"pin_groups": [{"pin_type": "power"}]}) == "no_pdf"


def test_load_cached_self_heals(tmp_pdf, tmp_parsed):
    groups = [{"pin_type": "power", "supply_rail_name": "VCC", "supply_min": 3.0, "supply_max": 3.6}]
    _write_legacy_cache(tmp_pdf, groups, {"extractor": "manual"})
    loaded = r3.load_cached_pin_groups(tmp_pdf)
    assert loaded["provenance"]["source_hash"]                # self-healed on load
    assert loaded["extraction_meta"]["extractor"] == "manual"  # extractor preserved
    assert r3.check_cache_provenance(tmp_pdf, loaded) == "current"


# ── doc-identity sibling key (TODO-337 Phase 2b) — must be migration-free ────

_DOC_ID = {"class": "STRONG", "tier": 3, "depth": 1, "ratio": 0.769}


def test_doc_identity_is_persisted_as_a_provenance_sibling(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma",
                             doc_identity=_DOC_ID)
    prov = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())["provenance"]
    assert prov["doc_identity"] == _DOC_ID
    # The migration boundary: INSIDE parse_config would flip all 708 caches stale.
    assert "doc_identity" not in prov["parse_config"]


def test_doc_identity_omitted_when_not_supplied(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")
    prov = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())["provenance"]
    assert "doc_identity" not in prov      # absent, never a guessed default


def test_doc_identity_does_not_change_source_hash(tmp_pdf, tmp_parsed):
    with_id = r3.build_provenance(tmp_pdf, doc_identity=_DOC_ID)
    without = r3.build_provenance(tmp_pdf)
    assert with_id["source_hash"] == without["source_hash"]
    assert with_id["parse_config"] == without["parse_config"]


def test_doc_identity_does_not_change_staleness_verdict(tmp_pdf, tmp_parsed):
    """Fresh/stale determination must be identical with and without the key."""
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")
    plain = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma",
                             doc_identity=_DOC_ID)
    stamped = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    assert r3.check_cache_provenance(tmp_pdf, plain) == "current"
    assert r3.check_cache_provenance(tmp_pdf, stamped) == "current"
    # ...and an existing (doc_identity-free) cache stays 'current' too — adding the
    # key to the writer does not retro-stale caches written before it existed.
    legacy = dict(plain)
    legacy["provenance"] = {k: v for k, v in plain["provenance"].items()
                            if k != "doc_identity"}
    assert r3.check_cache_provenance(tmp_pdf, legacy) == "current"


def test_doc_identity_survives_a_stale_signal_unchanged(tmp_pdf, tmp_parsed, monkeypatch):
    """A genuinely stale cache still reads stale — the key neither masks nor mints."""
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma",
                             doc_identity=_DOC_ID)
    cached = json.loads(r3.get_pin_groups_cache_path(tmp_pdf).read_text())
    monkeypatch.setattr(r3, "TARGET_PHRASES", r3.TARGET_PHRASES + ["recommended operating"])
    assert r3.check_cache_provenance(tmp_pdf, cached) == "stale"


# ── TODO-367 d3: pre_reextract sidecar on the overwrite path ────────────────

def _sidecar_path(cache_path):
    return cache_path.with_name(cache_path.name + ".pre_reextract")


def test_overwrite_creates_sidecar_with_old_bytes(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": [{"group_name": "OLD"}]}, extractor="gemma")
    cache_path = r3.get_pin_groups_cache_path(tmp_pdf)
    old_bytes = cache_path.read_bytes()

    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": [{"group_name": "NEW"}]}, extractor="gemma")

    sidecar = _sidecar_path(cache_path)
    assert sidecar.exists()
    assert sidecar.read_bytes() == old_bytes
    assert json.loads(cache_path.read_text())["pin_groups"] == [{"group_name": "NEW"}]


def test_second_overwrite_replaces_sidecar(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": [{"group_name": "V1"}]}, extractor="gemma")
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": [{"group_name": "V2"}]}, extractor="gemma")
    cache_path = r3.get_pin_groups_cache_path(tmp_pdf)
    sidecar = _sidecar_path(cache_path)
    assert json.loads(sidecar.read_text())["pin_groups"] == [{"group_name": "V1"}]

    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": [{"group_name": "V3"}]}, extractor="gemma")
    # single-level: the sidecar now holds V2 (the immediately-prior cache), not V1.
    assert json.loads(sidecar.read_text())["pin_groups"] == [{"group_name": "V2"}]
    assert json.loads(cache_path.read_text())["pin_groups"] == [{"group_name": "V3"}]


def test_fresh_write_creates_no_sidecar(tmp_pdf, tmp_parsed):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": [{"group_name": "FIRST"}]}, extractor="gemma")
    cache_path = r3.get_pin_groups_cache_path(tmp_pdf)
    assert not _sidecar_path(cache_path).exists()


def test_quarantine_refusal_creates_no_sidecar(tmp_pdf, tmp_parsed, monkeypatch):
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": [{"group_name": "RICH"}]},
                             extractor="manual", parse_quality="full_precap")
    monkeypatch.setattr(r3, "MAX_PDF_SIZE_FOR_MINERU", 1)  # current code would degrade
    r3.save_pin_groups_cache(tmp_pdf, {"pin_groups": []}, extractor="gemma")  # refused
    cache_path = r3.get_pin_groups_cache_path(tmp_pdf)
    assert not _sidecar_path(cache_path).exists()
    assert json.loads(cache_path.read_text())["pin_groups"] == [{"group_name": "RICH"}]  # unchanged
