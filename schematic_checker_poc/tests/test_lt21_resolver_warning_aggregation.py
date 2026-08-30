"""LT-21: resolver console-warning aggregation.

`pipeline.run_board`'s per-component resolve loop used to print one stderr line per
too-short-MPN miss (every generic passive value trips it) and included the full
PDFs-searched directory listing on every 'No datasheet found' miss. Both are now
formatted by small pure helpers so the aggregation/stripping logic is unit-testable
without running the pipeline (no resolver/network involved)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _format_no_datasheet_warning, _format_short_mpn_warning


def test_no_datasheet_warning_omits_pdfs_searched_listing():
    line = _format_no_datasheet_warning("Conn_01x04_Pin")
    assert line == "[STEP 03] WARNING: No datasheet found for 'Conn_01x04_Pin'."
    assert "PDFs searched" not in line


def test_short_mpn_warning_aggregates_count_and_values():
    line = _format_short_mpn_warning(["22u", "10u", "100n"])
    assert line == (
        "[STEP 03] WARNING: 3 components without MPN fields "
        "(22u, 10u, 100n) — resolved as generic passives, skipped."
    )


def test_short_mpn_warning_single_value():
    line = _format_short_mpn_warning(["RED"])
    assert line == (
        "[STEP 03] WARNING: 1 components without MPN fields "
        "(RED) — resolved as generic passives, skipped."
    )
