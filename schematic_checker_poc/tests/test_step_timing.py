"""Unit tests for pipeline.py's per-step wall-clock timing (TODO-223 jetson
wall-time attribution).

Covers: step_times_seconds is attached to a successful report with the expected
step keys, its sum is approximately the driver's own wall_time_seconds, and
run_checks's summary aggregation sums it correctly across boards. Uses a
small, cache-warm, in-manifest board (logs/v2_11_boards.txt) so the test runs
fast with 0 live network/LLM calls.
"""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
POC = REPO / "schematic_checker_poc"
sys.path.insert(0, str(POC))
sys.path.insert(0, str(REPO))

import pipeline  # noqa: E402
import run_checks as rct  # noqa: E402

BOARD = (REPO / "netlist_corpus" / "exported" / "avr_arduino" / "KiCad-Arduino-Boards"
          / "KiCad Projects" / "Arduino Leonardo" / "Headers.net")
pytestmark = pytest.mark.skipif(not BOARD.exists(), reason="fixture board not on disk")

EXPECTED_STEPS = {
    "02_parse", "03_resolve", "06_power", "07_confirm", "07b_passive",
    "04b_05_extract", "08_checker", "08b_supply", "08c_structural",
    "08d_peripheral", "08e_pullup", "08f_output_conflict",
    "08g_pullup_presence", "10_report",
}

_PRODUCTION_PARSED_DIR = POC / "datasheets_parsed"


def _board_cache_warm_parts(board: Path) -> list[str]:
    """Every distinct (comp (value "...")) on the board that has a matching
    production datasheets_parsed/<value> cache dir — mirrors
    test_resolve_memo.py's helper of the same name/purpose."""
    text = board.read_text()
    values = set(re.findall(r'\(value "([^"]*)"\)', text))
    # Match canonically: post-TODO-321 the production cache dir is lower-cased
    # regardless of the (case-varied) schematic value string.
    return [v for v in values if v and (_PRODUCTION_PARSED_DIR / v.lower()).is_dir()]


def _copy_cache_canonicalized(src: Path, dst: Path, stem: str) -> None:
    """Copy a production cache dir into a canonical (lower-cased) location AND
    lower-case the stem prefix of every file inside it. The resolver derives
    BOTH the cache-dir name and the inner .md / _pin_groups.json filenames from
    canonical_pdf_stem() (TODO-321), so a verbatim-cased inner filename is a
    cache miss exactly like a verbatim-cased dir would be — the copy must be
    fully canonical for the warm-cache path to be exercised."""
    shutil.copytree(src, dst)
    canon = stem.lower()
    if canon == stem:
        return
    for p in list(dst.rglob("*")):
        if p.is_file() and p.name.startswith(stem):
            p.rename(p.with_name(canon + p.name[len(stem):]))


def _run(tmp_path, monkeypatch):
    """PARSED_DIR isolation (TODO-227 Phase 2b follow-up): run_one's
    _init_resolver -> pipeline.configure_resolver unconditionally resets
    step_03_resolver.PARSED_DIR to pipeline.DEFAULT_PARSED_DIR on every call,
    so monkeypatching step_03_resolver.PARSED_DIR directly would be undone
    before resolve_and_parse ever runs. Instead copy each board part's real
    (cache-warm) production cache dir into a tmp location and monkeypatch
    pipeline.DEFAULT_PARSED_DIR itself, so the real resolve_and_parse still
    hits a warm cache (same read path, 0 live network/MinerU/pdfplumber
    calls) but any incidental write (e.g. a Branch-B patch_state.json
    refresh) lands in the tmp copy, never in production datasheets_parsed/."""
    isolated_parsed = tmp_path / "datasheets_parsed_isolated"
    isolated_parsed.mkdir()
    cache_warm_parts = _board_cache_warm_parts(BOARD)
    assert cache_warm_parts, "no cache-warm parts found for this board — isolation setup is stale"
    for part in cache_warm_parts:
        _copy_cache_canonicalized(
            _PRODUCTION_PARSED_DIR / part.lower(), isolated_parsed / part.lower(), part.lower())
    monkeypatch.setattr(pipeline, "DEFAULT_PARSED_DIR", str(isolated_parsed))

    return rct.run_one(
        netlist_path=BOARD, corpus_dir=REPO / "netlist_corpus",
        datasheets_dir=REPO / "netlist_corpus" / "datasheets",
        output_path=tmp_path / "r.json", skip_confirm=True)


def test_step_times_present_and_keys_match(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch)
    assert result["status"] != "pipeline_error"
    step_times = result["step_times_seconds"]
    assert set(step_times.keys()) == EXPECTED_STEPS
    assert all(isinstance(v, float) and v >= 0 for v in step_times.values())


def test_step_times_sum_approx_wall_time(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch)
    total_step_time = sum(result["step_times_seconds"].values())
    wall_time = result["wall_time_seconds"]
    # step_times is a subset of the driver's wall clock (chdir/mkdir/log-context
    # setup outside the marks aren't counted) — must not exceed it, and should
    # dominate it on a cache-warm run.
    assert total_step_time <= wall_time + 0.5
    assert total_step_time >= wall_time * 0.5


def test_pipeline_error_result_has_empty_step_times():
    result = rct._pipeline_error_result("step_02", "boom", __import__("time").monotonic())
    assert result["step_times_seconds"] == {}


def test_write_summary_aggregates_step_times_totals(tmp_path):
    run_results = [
        {"status": "all_pass", "step_times_seconds": {"02_parse": 1.0, "03_resolve": 2.0}},
        {"status": "all_pass", "step_times_seconds": {"02_parse": 0.5, "03_resolve": 3.5}},
        {"status": "pipeline_error", "step_times_seconds": {}},
    ]
    rct._write_summary(run_results, [], tmp_path, tmp_path, tmp_path, 3, 10.0)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["step_times_totals_seconds"] == {"02_parse": 1.5, "03_resolve": 5.5}
