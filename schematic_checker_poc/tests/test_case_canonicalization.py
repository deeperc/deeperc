"""Unit tests for TODO-321 Phase 1: canonical casefold at cache-path derivation.

canonical_pdf_stem() is the single shared helper every datasheets_parsed/ path
derivation site in step_03_resolver.py (and, via a local import, provenance.py)
goes through — see the recon (investigation/recon_reports/todo321_case_dup_
recon.md) for the full site inventory. These tests cover the helper itself and
the bifurcation guard (_warn_on_case_collision) that fires when a pre-existing,
differently-cased sibling directory is found at cache-write time.

Case-duplicate freshness-check coverage (the provenance stored-pdf_path
mechanism, recon section 2) lives in test_report_provenance.py, alongside the
rest of check_report_provenance's coverage — check_report_provenance takes
parsed_dir as an explicit argument, so no PARSED_DIR/DEFAULT_PARSED_DIR
isolation is needed there.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import step_03_resolver as r3  # noqa: E402


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    datasheets = tmp_path / "datasheets"
    parsed = tmp_path / "parsed"
    datasheets.mkdir()
    monkeypatch.setattr(r3, "DATASHEETS_DIR", str(datasheets))
    monkeypatch.setattr(r3, "PARSED_DIR", str(parsed))
    # TODO-386 Phase 3: extraction writes default to STAGING_DIR (R-C). Both
    # roots point at the same tmp dir so these tests keep exercising case
    # canonicalization rather than tier routing (tests/test_staging_tier.py).
    monkeypatch.setattr(r3, "STAGING_DIR", str(parsed))
    # TODO-388 Phase 2 (R-β): reconcile now MOVES case-variant directories into
    # the quarantine root instead of leaving them in place, so that root needs
    # the same isolation the other two get — otherwise these tests would move
    # tmp trees into the real schematic_checker_poc/datasheets_quarantine/.
    monkeypatch.setattr(r3, "QUARANTINE_DIR", str(tmp_path / "quarantine"))
    return datasheets, parsed


# ── canonical_pdf_stem ────────────────────────────────────────────────────────

def test_canonical_pdf_stem_lowercases_mixed_case():
    assert r3.canonical_pdf_stem("/ds/AP2112K-3.3.pdf") == "ap2112k-3.3"


def test_canonical_pdf_stem_idempotent_on_already_lower():
    assert r3.canonical_pdf_stem("/ds/bss138.pdf") == "bss138"


def test_canonical_pdf_stem_all_upper():
    assert r3.canonical_pdf_stem("/ds/TPS65988DHRSHR.pdf") == "tps65988dhrshr"


def test_canonical_pdf_stem_ignores_directory_and_extension_case():
    # Only the stem (basename minus extension) is canonicalized — the directory
    # and extension are not part of the derived cache key at all.
    assert r3.canonical_pdf_stem("/Some/DIR/Path/Esp32-S3-MINI-1-N8.PDF") == "esp32-s3-mini-1-n8"


def test_canonical_pdf_stem_preserves_non_alpha_shape():
    # Digits/hyphens/dots pass through unchanged — only letters fold.
    assert r3.canonical_pdf_stem("/ds/SN74AXC8T245RJWR.pdf") == "sn74axc8t245rjwr"
    assert r3.canonical_pdf_stem("/ds/2N7002KT1G.pdf") == "2n7002kt1g"


def test_canonical_pdf_stem_matches_across_case_variants():
    # The whole point of the helper: two on-disk PDFs that are case-duplicates
    # of the same physical part must derive the SAME cache key.
    assert (r3.canonical_pdf_stem("/ds/AO3407.pdf")
            == r3.canonical_pdf_stem("/ds/ao3407.pdf")
            == "ao3407")


# ── bifurcation guard (_warn_on_case_collision) ──────────────────────────────

def test_warn_on_case_collision_fires_via_save_pin_groups_cache(isolated_dirs, caplog):
    datasheets, parsed = isolated_dirs
    (parsed / "PARTA").mkdir(parents=True)  # legacy pre-canonicalization sibling
    pdf_path = str(datasheets / "PartA.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"fake pdf bytes")

    with caplog.at_level(logging.WARNING, logger="steps.step_03_resolver"):
        r3.save_pin_groups_cache(pdf_path, {"pin_groups": []}, extractor="gemma")

    warnings = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    todo_warnings = [m for m in warnings if "TODO-321" in m]
    assert len(todo_warnings) == 1
    assert "PARTA" in todo_warnings[0] and "parta" in todo_warnings[0]
    # canonical (lower-cased) write still succeeds — guard never blocks
    assert (parsed / "parta" / "auto" / "parta_pin_groups.json").exists()
    # legacy sibling is left untouched, never deleted
    assert (parsed / "PARTA").exists()


def test_warn_on_case_collision_silent_when_no_sibling(isolated_dirs, caplog):
    datasheets, parsed = isolated_dirs
    pdf_path = str(datasheets / "PartB.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"fake pdf bytes")

    with caplog.at_level(logging.WARNING, logger="steps.step_03_resolver"):
        r3.save_pin_groups_cache(pdf_path, {"pin_groups": []}, extractor="gemma")

    warnings = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    assert not any("TODO-321" in m for m in warnings)


def test_warn_on_case_collision_direct_call(isolated_dirs, caplog):
    """Unit test of the guard helper in isolation, independent of any caller."""
    _datasheets, parsed = isolated_dirs
    (parsed / "FOOBAR").mkdir(parents=True)

    with caplog.at_level(logging.WARNING, logger="steps.step_03_resolver"):
        r3._warn_on_case_collision("/ds/foobar.pdf")

    warnings = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any("TODO-321" in m for m in warnings)


def test_warn_on_case_collision_no_parsed_dir_yet_is_silent(isolated_dirs, caplog):
    # PARSED_DIR doesn't exist at all yet (first-ever cache write) — os.listdir
    # raises FileNotFoundError, which the guard must swallow, not propagate.
    _datasheets, _parsed = isolated_dirs
    with caplog.at_level(logging.WARNING, logger="steps.step_03_resolver"):
        r3._warn_on_case_collision("/ds/anything.pdf")
    warnings = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    assert not any("TODO-321" in m for m in warnings)


def test_warn_on_case_collision_dest_path_override_skips_guard(isolated_dirs, caplog, tmp_path):
    # dest_path (scratch/dry-run preview) never touches the real datasheets_parsed
    # cache, so the collision check against PARSED_DIR must not fire for it.
    datasheets, parsed = isolated_dirs
    (parsed / "PARTC").mkdir(parents=True)
    pdf_path = str(datasheets / "PartC.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"fake pdf bytes")
    scratch = tmp_path / "scratch_preview.json"

    with caplog.at_level(logging.WARNING, logger="steps.step_03_resolver"):
        r3.save_pin_groups_cache(pdf_path, {"pin_groups": []}, extractor="gemma",
                                 dest_path=str(scratch))

    warnings = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    assert not any("TODO-321" in m for m in warnings)
    assert scratch.exists()


# ── MinerU output reconciliation (_reconcile_mineru_output) ──────────────────

def _fake_mineru_output(parsed, stem):
    """Build a faked MinerU output tree PARSED_DIR/<stem>/auto/ with a .md file
    and a sibling image, mirroring what `magic-pdf -o PARSED_DIR` writes."""
    auto = parsed / stem / "auto"
    auto.mkdir(parents=True)
    (auto / f"{stem}.md").write_text("![](images/fig1.jpg)\nparsed body\n")
    (auto / "images").mkdir()
    (auto / "images" / "fig1.jpg").write_bytes(b"\xff\xd8fakejpg")


def test_reconcile_moves_verbatim_dir_to_canonical(isolated_dirs):
    # MinerU wrote to the PDF's on-disk (upper) case; canonical dir absent.
    datasheets, parsed = isolated_dirs
    _fake_mineru_output(parsed, "PPTC101LFBN-RC")
    pdf_path = str(datasheets / "PPTC101LFBN-RC.pdf")

    r3._reconcile_mineru_output(pdf_path)

    # whole-dir move: canonical dir now holds the .md AND the relative image ref
    assert (parsed / "pptc101lfbn-rc" / "auto" / "pptc101lfbn-rc.md").exists() is False
    # NB the .md file itself keeps its verbatim basename inside the moved tree —
    # reconciliation renames the DIRECTORY, not the files within it.
    assert (parsed / "pptc101lfbn-rc" / "auto" / "PPTC101LFBN-RC.md").exists()
    assert (parsed / "pptc101lfbn-rc" / "auto" / "images" / "fig1.jpg").exists()
    # verbatim dir is gone (moved, not copied)
    assert not (parsed / "PPTC101LFBN-RC").exists()


def test_reconcile_both_exist_with_canonical_auto_quarantines_verbatim(isolated_dirs, caplog, tmp_path):
    """TODO-388 R-β, GENUINE-CONFLICT branch (canonical already holds auto/).

    Inverted from the pre-R-β assertion that the verbatim sibling SURVIVES in
    place: surviving in place is exactly what made the stem permanently
    ambiguous to find_cached_dir_for_part. Canonical is still authoritative and
    still untouched — the sibling is now MOVED to quarantine, never deleted.
    """
    datasheets, parsed = isolated_dirs
    _fake_mineru_output(parsed, "PPTC101LFBN-RC")
    (parsed / "pptc101lfbn-rc" / "auto").mkdir(parents=True)  # canonical already present
    (parsed / "pptc101lfbn-rc" / "auto" / "canonical.md").write_text("authoritative\n")
    pdf_path = str(datasheets / "PPTC101LFBN-RC.pdf")

    with caplog.at_level(logging.WARNING, logger="steps.step_03_resolver"):
        r3._reconcile_mineru_output(pdf_path)

    todo = [r.message for r in caplog.records
            if r.levelno == logging.WARNING and "TODO-388" in r.message]
    assert len(todo) == 1
    # canonical is authoritative and byte-untouched
    assert (parsed / "pptc101lfbn-rc" / "auto" / "canonical.md").read_text() == "authoritative\n"
    # the case-variant sibling no longer exists under the cache root at all —
    # that is the whole point: one stem, one directory, no ambiguity
    assert not (parsed / "PPTC101LFBN-RC").exists()
    # ...but it was MOVED, not deleted: every file is still inspectable
    q = tmp_path / "quarantine" / "PPTC101LFBN-RC"
    assert (q / "auto" / "PPTC101LFBN-RC.md").exists()
    assert (q / "auto" / "images" / "fig1.jpg").exists()


def test_reconcile_merges_into_canonical_lacking_auto(isolated_dirs, tmp_path):
    """TODO-388 R-β, MERGE branch — the common real-world shape.

    A canonical directory holding ONLY pdfplumber/ (written case-safely by the
    fallback branch on an earlier run where MinerU was unavailable) is not a
    conflict. MinerU's auto/ tree merges in beside it, and the emptied shell
    goes to quarantine rather than being rmdir'd.
    """
    datasheets, parsed = isolated_dirs
    _fake_mineru_output(parsed, "EG1218")
    pdfp = parsed / "eg1218" / "pdfplumber"
    pdfp.mkdir(parents=True)
    (pdfp / "eg1218.md").write_text("fallback text\n")
    pdf_path = str(datasheets / "EG1218.pdf")

    r3._reconcile_mineru_output(pdf_path)

    # both trees now live under the ONE canonical directory
    assert (parsed / "eg1218" / "pdfplumber" / "eg1218.md").read_text() == "fallback text\n"
    assert (parsed / "eg1218" / "auto" / "EG1218.md").exists()
    assert (parsed / "eg1218" / "auto" / "images" / "fig1.jpg").exists()
    # no case-variant sibling survives
    assert not (parsed / "EG1218").exists()
    # the emptied shell was quarantined, not removed
    assert (tmp_path / "quarantine" / "EG1218").is_dir()


def test_reconcile_merge_never_clobbers_a_colliding_entry(isolated_dirs, tmp_path):
    """A verbatim entry whose name already exists canonically STAYS PUT — the
    canonical copy wins — and the remainder is quarantined with its content
    intact, so nothing is lost either way."""
    datasheets, parsed = isolated_dirs
    _fake_mineru_output(parsed, "PARTX")
    canon_auto = parsed / "partx" / "auto"
    canon_auto.mkdir(parents=True)
    (canon_auto / "keeper.md").write_text("canonical wins\n")
    # canonical has auto/ -> genuine-conflict branch, whole-dir quarantine
    r3._reconcile_mineru_output(str(datasheets / "PARTX.pdf"))
    assert (canon_auto / "keeper.md").read_text() == "canonical wins\n"
    assert (tmp_path / "quarantine" / "PARTX" / "auto" / "PARTX.md").exists()


def test_reconcile_quarantine_collision_suffixes_never_overwrites(isolated_dirs, tmp_path):
    """A stem that quarantines twice must not destroy the first tree."""
    datasheets, parsed = isolated_dirs
    for _ in range(2):
        _fake_mineru_output(parsed, "TWICE")
        (parsed / "twice" / "auto").mkdir(parents=True, exist_ok=True)
        r3._reconcile_mineru_output(str(datasheets / "TWICE.pdf"))

    q = tmp_path / "quarantine"
    assert (q / "TWICE" / "auto" / "TWICE.md").exists()
    assert (q / "TWICE-2" / "auto" / "TWICE.md").exists()


def test_reconcile_operates_against_an_explicit_output_root(isolated_dirs, tmp_path):
    """R-α: reconcile follows whatever root it is handed (the staging tier
    during a --staging run), not a hardcoded PARSED_DIR."""
    datasheets, _parsed = isolated_dirs
    staged = tmp_path / "staged"
    _fake_mineru_output(staged, "ROOTED")
    pdf_path = str(datasheets / "ROOTED.pdf")

    r3._reconcile_mineru_output(pdf_path, output_root=str(staged))

    assert (staged / "rooted" / "auto" / "ROOTED.md").exists()
    assert not (staged / "ROOTED").exists()


def test_reconcile_noop_when_already_lowercase(isolated_dirs):
    datasheets, parsed = isolated_dirs
    _fake_mineru_output(parsed, "bss138")  # PDF stem already canonical
    pdf_path = str(datasheets / "bss138.pdf")

    r3._reconcile_mineru_output(pdf_path)  # must not touch anything

    assert (parsed / "bss138" / "auto" / "bss138.md").exists()


def test_reconcile_silent_when_verbatim_dir_absent(isolated_dirs):
    # MinerU produced no output tree at all (failure); nothing to reconcile.
    datasheets, parsed = isolated_dirs
    parsed.mkdir(parents=True, exist_ok=True)
    pdf_path = str(datasheets / "NOTHING-HERE.pdf")

    r3._reconcile_mineru_output(pdf_path)  # no raise, no dir created

    assert not (parsed / "nothing-here").exists()
    assert not (parsed / "NOTHING-HERE").exists()
