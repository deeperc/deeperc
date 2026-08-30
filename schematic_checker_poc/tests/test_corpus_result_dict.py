"""Guards the Phase-3 corpus result-dict coupling (arc finding #2).

run_one builds its summary/baseline result dict from a report: COUNTS come from the
report's summary sub-dicts, STATUS from pipeline.classify_report (which reads the result
ARRAYS). On a real report these agree because build_report keeps summary⇔arrays
consistent. This test locks that agreement in with a build_report-shaped report, without
running the pipeline (hermetic).
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import run_checks as rct
from pipeline import classify_report


def _consistent_report(net, sup, st, per):
    """Report with mutually-consistent summary + result arrays (as build_report emits).
    Each arg is (pass, warn, fail, unres); structural drops unres."""
    def arr(counts, field, labels=("PASS", "WARN", "FAIL", "UNRESOLVABLE")):
        out = []
        for c, lab in zip(counts, labels):
            out += [{field: lab}] * c
        return out
    net_a = arr(net, "status")
    sup_a = arr(sup, "status")
    st_a = arr(st, "status", labels=("PASS", "WARN", "FAIL"))
    per_a = arr(per, "severity")
    return {
        "summary": {
            "total_nets_checked": len(net_a),
            "pass": net[0], "warn": net[1], "fail": net[2], "unresolvable": net[3],
            "supply_checks": {"total": len(sup_a), "pass": sup[0], "warn": sup[1],
                              "fail": sup[2], "unresolvable": sup[3]},
            "structural_checks": {"total": len(st_a), "pass": st[0], "warn": st[1], "fail": st[2]},
            "peripheral_checks": {"total": len(per_a), "pass": per[0], "warn": per[1],
                                  "fail": per[2], "unresolvable": per[3]},
        },
        "results": net_a,
        "power_supply_results": sup_a,
        "structural_integrity_results": st_a,
        "peripheral_integrity_results": per_a,
        "pullup_value_results": [],
    }


def test_result_dict_status_matches_classifier_and_counts_match_summary():
    rep = _consistent_report(net=(3, 1, 2, 0), sup=(1, 0, 1, 2), st=(4, 0, 1, 0), per=(0, 0, 2, 0))
    r = rct._result_from_report_dict(rep, t0=0.0)
    # STATUS comes from classify_report (array-driven) — must equal a direct call.
    assert r["status"] == classify_report(rep) == "has_fail"
    # COUNTS come from the summary — must equal what the summary holds.
    s = rep["summary"]
    assert (r["fail_count"], r["warn_count"], r["pass_count"], r["unresolvable_count"]) == (2, 1, 3, 0)
    assert r["nets_checked"] == s["total_nets_checked"] == 6
    assert (r["supply_fail"], r["supply_unresolvable"]) == (1, 2)
    assert r["structural_fail"] == 1
    assert r["peripheral_fail"] == 2


def test_result_dict_unresolvable_only_agrees():
    # supply UNRESOLVABLE only, nothing else → has_unresolvable_only on both axes.
    rep = _consistent_report(net=(0, 0, 0, 0), sup=(0, 0, 0, 2), st=(0, 0, 0, 0), per=(0, 0, 0, 0))
    r = rct._result_from_report_dict(rep, t0=0.0)
    assert r["status"] == classify_report(rep) == "has_unresolvable_only"
    assert r["supply_unresolvable"] == 2


def test_pipeline_error_result_shape():
    r = rct._pipeline_error_result("step_02", "boom", t0=0.0)
    assert r["status"] == "pipeline_error"
    assert r["failed_at_step"] == "step_02"
    assert r["fail_count"] == 0 and r["nets_checked"] == 0


def test_board_console_summary_surfaces_peripheral_fail_with_zero_signal_fails():
    # The acruxcz shape: 0 signal-bucket FAILs but a peripheral FAIL — classify_report
    # calls this has_fail, but the old --board printer read only fail_count/warn_count/
    # pass_count (the signal bucket) and displayed "0 FAIL", hiding it. The console
    # summary must surface the aggregate status, not just the signal-bucket count.
    rep = _consistent_report(net=(0, 0, 0, 0), sup=(0, 0, 0, 0), st=(0, 0, 0), per=(0, 0, 1, 0))
    r = rct._result_from_report_dict(rep, t0=0.0)
    assert r["status"] == "has_fail"
    assert r["fail_count"] == 0   # signal bucket alone still reads 0 — that's the trap
    summary_text = rct._format_board_console_summary(r)
    assert "has_fail" in summary_text
    assert "1 FAIL" in summary_text   # the peripheral FAIL must be visible somewhere
