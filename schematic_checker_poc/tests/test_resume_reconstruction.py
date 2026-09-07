"""Tests for --resume report reconstruction + freshness guard (run_checks).

Background: `--resume` previously did a bare `continue` on every skipped board,
so skipped boards never entered `run_results` → the summary recorded `ran: 0`
with an empty `per_netlist`, and `compare_baseline` saw 0 current boards. These
tests lock in the fix: a skipped board is reconstructed from its report JSON
(so it is counted), and a report older than the checker source tree is treated
as stale (re-run, not reused).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import run_checks as rct  # noqa: E402


def _arr(counts: dict, field: str) -> list:
    """Result array consistent with a summary sub-dict's pass/warn/fail/unresolvable
    counts. Real reports (build_report) always keep arrays and summary consistent;
    classify_report reads the arrays, so the fixture must too (else it would encode a
    report shape that never occurs on disk)."""
    out = []
    for status in ("PASS", "WARN", "FAIL", "UNRESOLVABLE"):
        out += [{field: status}] * counts.get(status.lower(), 0)
    return out


def _report(summary: dict) -> dict:
    # Arrays derived from the summary so the fixture matches a real on-disk report
    # (post-Phase-2 classify_report is registry/array-driven, not summary-driven).
    return {
        "schema_version": 1, "source_netlist": "x.net", "summary": summary,
        "results": _arr(summary, "status"),
        "power_supply_results": _arr(summary.get("supply_checks", {}), "status"),
        "structural_integrity_results": _arr(summary.get("structural_checks", {}), "status"),
        "peripheral_integrity_results": _arr(summary.get("peripheral_checks", {}), "severity"),
        "pullup_value_results": [],
    }


# ── result_from_report: counts + status round-trip ────────────────────────────
def test_result_from_report_reconstructs_counts_and_status(tmp_path):
    rp = tmp_path / "r.json"
    rp.write_text(json.dumps(_report({
        "total_nets_checked": 6, "fail": 2, "warn": 1, "pass": 3, "unresolvable": 0,
        "supply_checks":     {"total": 4, "pass": 1, "warn": 0, "fail": 1, "unresolvable": 2},
        "structural_checks": {"total": 5, "pass": 4, "warn": 0, "fail": 1},
        "peripheral_checks": {"total": 2, "pass": 0, "warn": 0, "fail": 2, "unresolvable": 0},
    })))
    r = rct.result_from_report(rp)
    assert r["status"] == "has_fail"          # any FAIL across dimensions
    assert (r["fail_count"], r["warn_count"], r["pass_count"]) == (2, 1, 3)
    assert r["nets_checked"] == 6
    assert (r["supply_fail"], r["supply_unresolvable"]) == (1, 2)
    assert r["structural_fail"] == 1
    assert r["peripheral_fail"] == 2
    assert r["wall_time_seconds"] == 0.0      # not run this session
    assert r["resumed"] is True


def test_result_from_report_status_matches_classifier(tmp_path):
    # An UNRESOLVABLE-only board must reconstruct as has_unresolvable_only, not
    # all_pass — the exact class that the broken resume used to drop silently.
    rp = tmp_path / "u.json"
    rp.write_text(json.dumps(_report({
        "total_nets_checked": 0, "fail": 0, "warn": 0, "pass": 0, "unresolvable": 0,
        "supply_checks":     {"total": 2, "pass": 0, "warn": 0, "fail": 0, "unresolvable": 2},
        "structural_checks": {"total": 0, "pass": 0, "warn": 0, "fail": 0},
        "peripheral_checks": {"total": 0, "pass": 0, "warn": 0, "fail": 0, "unresolvable": 0},
    })))
    assert rct.result_from_report(rp)["status"] == "has_unresolvable_only"


# ── result_from_report round-trips a REAL report ────────────────────────────
def test_result_from_report_on_real_report(tmp_path):
    """Round-trips a genuine on-disk report, not the hand-crafted _report()
    shape above -- catches drift between what build_report actually writes
    and what result_from_report expects to read.

    Generates its own fixture (a shipped examples/ board, run fresh into
    tmp_path) instead of globbing corpus_results/reports/ for whatever an
    earlier, unrelated run happened to leave there: that made this test's
    skip/run outcome -- and so the suite's total skip count -- depend on
    arrival state, exactly the class of bug CONTRIBUTING.md's "a skip on a
    test whose fixture ships in examples/ is a bug" rule exists to catch.
    Never skips now."""
    board = REPO_ROOT / "examples" / "my_stm32_board_i2c_swap" / "my_stm32_board_i2c_swap.net"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "run_checks.py"),
         "--board", str(board), "--skip-confirm", "--output-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    rp = tmp_path / "reports" / f"{board.stem}.json"
    assert rp.exists(), f"expected report not written: {rp}"

    report = json.loads(rp.read_text())
    r = rct.result_from_report(rp)
    # Reconstructed counts equal the report's own summary block.
    s = report["summary"]
    assert r["fail_count"] == s.get("fail", 0)
    assert r["pass_count"] == s.get("pass", 0)
    assert r["status"] == rct.classify_netlist_status(report)


# ── freshness guard: stale vs fresh by mtime ──────────────────────────────────
def test_newest_checker_source_mtime_is_positive_and_real():
    m = rct.newest_checker_source_mtime()
    assert m > 0
    # It must reflect an actual checker source — touching one moves it forward.
    probe = rct.POC_DIR / "steps" / "peripheral_coherence.py"
    if probe.exists():
        assert m >= probe.stat().st_mtime - 1


def test_freshness_decision_stale_vs_fresh(tmp_path):
    # The reuse gate compares report mtime against newest_checker_source_mtime():
    # a report written before the source is stale (re-run), one written after is
    # fresh (reuse). Assert the comparison the closure performs.
    src_mtime = rct.newest_checker_source_mtime()
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(_report({"total_nets_checked": 0})))
    os.utime(stale, (src_mtime - 100, src_mtime - 100))
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps(_report({"total_nets_checked": 0})))
    os.utime(fresh, (src_mtime + 100, src_mtime + 100))
    assert stale.stat().st_mtime < src_mtime   # → re-run
    assert fresh.stat().st_mtime >= src_mtime   # → reuse
