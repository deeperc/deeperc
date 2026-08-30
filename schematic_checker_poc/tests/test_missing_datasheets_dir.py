"""LT-20: an absent --datasheets-dir must WARN and proceed, never hard-exit.

Startup used to `sys.exit(1)` when the (possibly default) datasheets dir didn't
exist -- a crash a fresh clone can't diagnose (LT-1 recon finding A4), even
though the KB-driven peripheral check path needs no PDFs at all (LT-1 proved
this live). Fixed to WARN + continue.

TODO-362 Phase 1 SPLIT the original single-test contract in two, because a
missing --datasheets-dir no longer implies "every datasheet-backed check goes
UNRESOLVABLE" -- the PDF-optional warm-cache shortcut (pipeline._try_warm_cache_
shortcut) now resolves a part straight from a warm datasheets_parsed/ cache
even when there is no PDF store to walk at all:

  - test_missing_datasheets_dir_resolves_from_pdf_free_cache (below, subprocess
    -based, this module's original mechanism preserved) is now the PDF-FREE
    CONTRACT test: --datasheets-dir missing + warm production caches ->
    the board resolves via the shortcut, supply checks are NOT degraded.
  - test_missing_datasheets_dir_true_zero_resolution_degrades (below, in-
    process, monkeypatch-based) re-anchors LT-20's original "true 0-PDF run"
    spirit: --datasheets-dir missing AND the cache-dir locator forced to MISS
    (same seam test_resolve_memo.py's sibling fix uses) -> now nothing can
    resolve, and datasheet-backed checks degrade to UNRESOLVABLE exactly as
    LT-20 originally proved. This needs the in-process pipeline.run_board
    entry point rather than a subprocess: monkeypatching a module attribute
    (find_cached_dir_for_part) cannot reach across a subprocess boundary, and
    there is no --parsed-dir / env-var seam in run_checks.py or
    pipeline.configure_resolver to isolate PARSED_DIR from a subprocess
    (grepped both -- neither takes one; configure_resolver unconditionally
    sets step_03_resolver.PARSED_DIR = DEFAULT_PARSED_DIR). Adding one would
    be a product-code change outside this cycle's frozen diff, so the
    in-process route is used instead of inventing that seam here.

L2 vendor lookup creds are stripped from the subprocess env (test 1 only) so
an L1 miss on the board's real MCU (STM32F103C8T6) fails locally (no
credentials -> no network call) instead of placing a live API call -- this
test must stay hermetic.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
POC = REPO / "schematic_checker_poc"
BOARD = REPO / "netlist_corpus/stm32/community/shadaab1904-STM32-PCB-Design/My_STM32_Board.net"
pytestmark = pytest.mark.skipif(not BOARD.exists(), reason="anchor board fixture not on disk")

sys.path.insert(0, str(POC))
sys.path.insert(0, str(REPO))

# See test_missing_corpus_dir.py's identical fixture for why this degrades to
# a no-op strip (and why that's correct) when the private-tier L2 vendor
# seam isn't importable.
try:
    from steps import step_03_l2_ext as _l2_ext
    _CREDENTIAL_ENV_VARS = _l2_ext.CREDENTIAL_ENV_VARS
except Exception:
    _CREDENTIAL_ENV_VARS = ()

_ISOLATED_ENV = {
    k: v for k, v in os.environ.items() if k not in _CREDENTIAL_ENV_VARS
}


def _run(datasheets_dir, output_dir):
    return subprocess.run(
        [sys.executable, str(REPO / "run_checks.py"), "--board", str(BOARD),
         "--datasheets-dir", str(datasheets_dir), "--skip-confirm",
         "--output-dir", str(output_dir)],
        capture_output=True, text=True, env=_ISOLATED_ENV)


def test_missing_datasheets_dir_resolves_from_pdf_free_cache(tmp_path):
    """TODO-362 Phase 1 PDF-FREE CONTRACT: --datasheets-dir missing + a warm
    production datasheets_parsed/ cache -> the board resolves via the
    PDF-optional shortcut, so datasheet-backed (supply) checks are NOT
    degraded to UNRESOLVABLE the way they were pre-fix.

    TODO-368 Phase 2 closes the surfacing gap this test's docstring used to
    flag: step_03_resolver.check_cache_provenance's pdf_path=None branch
    returns "current_no_pdf_verify" (still LOG-ONLY as a staleness detector —
    it never auto-invalidates), which load_cached_pin_groups now captures as
    `cache_currency` and pipeline.run_board threads into extraction_metadata.
    step_10_report's evidence-tier layer maps it to tier-2 ("cache_unverified"):
    every resolved part's extraction_metadata STILL carries pdf_path=null (no
    PDF was ever opened) and mineru_used=false (no parse ran) — that part of
    the original assertion is unchanged — but the report's cache-derived
    findings now ALSO carry evidence_tier="cache_unverified" and their
    evidence_label ends with the frozen tier-2 parenthetical.
    """
    missing = tmp_path / "no_such_datasheets_dir"
    assert not missing.exists()

    r = _run(missing, tmp_path)

    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    assert f"WARN: datasheets dir not found: {missing}" in r.stderr
    assert "ERROR: datasheets dir not found" not in r.stderr

    report = json.loads((tmp_path / "reports" / "My_STM32_Board.json").read_text())
    summary = report["summary"]
    assert summary["structural_checks"]["total"] > 0
    assert summary["structural_checks"]["fail"] == 0
    assert summary["supply_checks"]["total"] > 0
    assert summary["supply_checks"]["unresolvable"] == 0

    extraction_metadata = report["extraction_metadata"]
    resolved_parts = {
        part: meta for part, meta in extraction_metadata.items()
        if "mineru_used" in meta  # only resolved parts carry this key (see run_board)
    }
    assert resolved_parts, "no parts resolved — the PDF-free contract this test targets never fired"
    for part, meta in resolved_parts.items():
        assert meta["pdf_path"] is None, f"{part}: expected no PDF opened, got {meta['pdf_path']!r}"
        assert meta["mineru_used"] is False, f"{part}: expected no MinerU parse"
        assert meta["cache_currency"] == "current_no_pdf_verify", (
            f"{part}: expected the PDF-free check_cache_provenance status, "
            f"got {meta.get('cache_currency')!r}")

    supply_entries = report["power_supply_results"]
    assert supply_entries, "no supply findings — the tier-2 assertion below has no coverage"
    for entry in supply_entries:
        assert entry["evidence_tier"] == "cache_unverified", (
            f"{entry['refdes']}/{entry['part_number']}: expected cache_unverified "
            f"(the part resolved with pdf_path=None), got {entry['evidence_tier']!r}")
        assert entry["evidence_label"].endswith(" (not locally verified)"), (
            f"{entry['refdes']}/{entry['part_number']}: evidence_label missing the "
            f"frozen tier-2 parenthetical: {entry['evidence_label']!r}")


def test_missing_datasheets_dir_true_zero_resolution_degrades(tmp_path, monkeypatch):
    """LT-20's original contract, re-anchored (TODO-362 Phase 1): with
    --datasheets-dir missing AND the PDF-optional cache-dir locator forced to
    MISS (same seam as test_resolve_memo.py's
    test_shared_doc_not_mutated_by_real_consumers fix), nothing can resolve —
    datasheet-backed checks degrade to UNRESOLVABLE exactly as LT-20
    originally proved, and the run still doesn't crash. In-process (see the
    module docstring for why a subprocess can't reach this monkeypatch)."""
    import pipeline
    from steps import step_03_resolver

    monkeypatch.setattr(step_03_resolver, "find_cached_dir_for_part", lambda part_number: None)
    # Hermetic: force the L2 network tier off directly (module-level memoization
    # of _l2_resolve_ok/_l2_import_ok means env-var stripping alone is not
    # reliable if an earlier test in this process already probed it with real
    # credentials present) — this test must make 0 live network calls, same
    # requirement the subprocess-based sibling test enforces via env isolation.
    monkeypatch.setattr(step_03_resolver, "_l2_resolve_ok", lambda: False)

    missing = tmp_path / "no_such_datasheets_dir"
    assert not missing.exists()

    ctx = pipeline.PipelineContext(
        netlist_path=str(BOARD), skip_confirm=True,
        datasheets_dir=str(missing), output_path=str(tmp_path / "r.json"))
    out = pipeline.run_board(ctx)
    assert not isinstance(out, pipeline.PipelineFailure), out

    summary = out["summary"]
    assert summary["structural_checks"]["total"] > 0
    assert summary["structural_checks"]["fail"] == 0
    assert summary["supply_checks"]["unresolvable"] == summary["supply_checks"]["total"] > 0
    assert summary["supply_checks"]["pass"] == 0


def test_present_datasheets_dir_unaffected(tmp_path):
    """Sanity companion to the missing-dir case: with the dir present, the
    startup path is byte-identical to before this change (no WARN, no message
    about the datasheets dir at all beyond the normal banner line)."""
    present = REPO / "netlist_corpus" / "datasheets"
    r = _run(present, tmp_path)

    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    assert "WARN: datasheets dir not found" not in r.stderr
    assert "ERROR: datasheets dir not found" not in r.stderr
