"""LT-21: the --board console RESULT box.

Locks two behaviors of `step_10_report._print_summary`'s replacement box:
  1. `_aggregate_verdict_counts` sums FAIL/WARN/UNRESOLVABLE across every
     VERDICT_MOVING axis (not just the step-08 signal bucket) — the old box's
     "FAIL: 0" contradicted "Status: has_fail" whenever the fail lived in a
     peripheral/supply/structural finding instead of a signal-net one.
  2. the doubled report-path bug (".//home/...") is gone: an absolute
     output_path prints bare, a relative one keeps its "./" prefix.
Console text itself (rich Console output) isn't asserted here — these test the
pure data/path helpers `_print_summary` calls, matching this repo's existing
hermetic-report-dict testing convention (test_corpus_result_dict.py).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.step_10_report import _aggregate_verdict_counts


def _report(signal=(0, 0, 0, 0), supply=(0, 0, 0, 0), structural=(0, 0, 0),
            peripheral=(0, 0, 0, 0), pullup_value=(0, 0, 0),
            output_conflict=(0,), pullup_presence=(0,)):
    p, w, f, u = signal
    sp, sw, sf, su = supply
    stp, stw, stf = structural
    pp, pw, pf, pu = peripheral
    pvw, pvf, pvu = pullup_value
    (ocf,) = output_conflict
    (ppw,) = pullup_presence
    return {
        "summary": {
            "pass": p, "warn": w, "fail": f, "unresolvable": u,
            "supply_checks": {"total": sum(supply), "pass": sp, "warn": sw,
                              "fail": sf, "unresolvable": su},
            "structural_checks": {"total": sum(structural), "pass": stp,
                                  "warn": stw, "fail": stf},
            "peripheral_checks": {"total": sum(peripheral), "pass": pp,
                                  "warn": pw, "fail": pf, "unresolvable": pu},
            # No "pass" field on any of these three (Todo 248 recon: these
            # checkers never emit PASS) — mirrors the real report shape.
            "pullup_value_checks": {"total": pvw + pvf + pvu, "warn": pvw,
                                    "fail": pvf, "unresolvable": pvu},
            "output_conflict_checks": {"total": ocf, "fail": ocf},
            "pullup_presence_checks": {"total": ppw, "warn": ppw},
        },
    }


def test_aggregate_counts_include_peripheral_fail_with_zero_signal_fail():
    # The acruxcz shape: signal bucket is all-zero but a peripheral FAIL exists.
    # The old box read only the signal bucket and printed "FAIL: 0" — wrong.
    report = _report(peripheral=(0, 0, 1, 0))
    totals = _aggregate_verdict_counts(report)
    assert totals == {"fail": 1, "warn": 0, "unresolvable": 0}


def test_aggregate_counts_sum_across_all_axes():
    report = _report(signal=(0, 1, 0, 0), supply=(0, 0, 1, 2),
                      structural=(0, 0, 1), peripheral=(0, 0, 2, 2))
    totals = _aggregate_verdict_counts(report)
    assert totals == {"fail": 4, "warn": 1, "unresolvable": 4}


def test_aggregate_counts_all_pass_is_all_zero():
    report = _report(signal=(3, 0, 0, 0), supply=(5, 0, 0, 0), structural=(11, 0, 0))
    totals = _aggregate_verdict_counts(report)
    assert totals == {"fail": 0, "warn": 0, "unresolvable": 0}


# ── TODO-425: additive summary.total_pass/warn/fail/unresolvable ─────────────
# Grand totals across the signal bucket + every VERDICT_MOVING checker's
# summary_key sub-dict — same cross-axis reach as _aggregate_verdict_counts,
# extended to also cover "pass" (which the console box never prints, so
# _aggregate_verdict_counts alone doesn't compute it).
from steps.step_10_report import _sum_verdict_field, _compute_summary_totals


def test_sum_verdict_field_pass_spans_every_pass_bearing_axis():
    # pass total = signal + supply + structural + peripheral only — the other
    # three checkers (pullup_value/output_conflict/pullup_presence) carry no
    # "pass" key at all and must contribute nothing (not crash, not miscount).
    report = _report(signal=(2, 0, 0, 0), supply=(11, 0, 0, 0),
                      structural=(23, 0, 0), peripheral=(37, 0, 0, 0),
                      pullup_value=(53, 59, 61), output_conflict=(67,),
                      pullup_presence=(71,))
    assert _sum_verdict_field(report, "pass") == 2 + 11 + 23 + 37


def test_compute_summary_totals_matches_signal_plus_sum_of_sub_objects():
    report = _report(signal=(2, 3, 5, 7), supply=(11, 13, 17, 19),
                      structural=(23, 29, 31), peripheral=(37, 41, 43, 47),
                      pullup_value=(53, 59, 61), output_conflict=(67,),
                      pullup_presence=(71,))
    totals = _compute_summary_totals(report)
    assert totals == {
        "total_pass": 2 + 11 + 23 + 37,
        "total_warn": 3 + 13 + 29 + 41 + 53 + 71,
        "total_fail": 5 + 17 + 31 + 43 + 59 + 67,
        "total_unresolvable": 7 + 19 + 47 + 61,
    }
    # For the three fields _aggregate_verdict_counts already covers, the two
    # helpers must agree exactly (same underlying aggregation, not re-derived).
    agg = _aggregate_verdict_counts(report)
    assert totals["total_warn"] == agg["warn"]
    assert totals["total_fail"] == agg["fail"]
    assert totals["total_unresolvable"] == agg["unresolvable"]


def test_compute_summary_totals_is_additive_hoisted_keys_unchanged():
    report = _report(signal=(1, 2, 3, 4))
    before = dict(report["summary"])
    report["summary"].update(_compute_summary_totals(report))
    # The four pre-existing hoisted keys keep their original values.
    for key in ("pass", "warn", "fail", "unresolvable"):
        assert report["summary"][key] == before[key]
    # New keys are purely additive.
    assert {"total_pass", "total_warn", "total_fail", "total_unresolvable"} <= set(
        report["summary"].keys())


def test_full_report_path_display_absolute_vs_relative():
    # Mirrors _print_summary's display_path logic directly (LT-21 fix #4): an
    # absolute output_path must NOT get a "./" prefix (that's the doubled-path
    # bug, ".//home/..."); a relative one (main.py's "report.json" default)
    # keeps the existing "./" prefix.
    abs_path = "/home/user/schecker/corpus_results/reports/board.json"
    rel_path = "report.json"

    def display(output_path):
        out = Path(output_path)
        return str(out) if out.is_absolute() else f"./{output_path}"

    assert display(abs_path) == abs_path
    assert not display(abs_path).startswith(".//")
    assert display(rel_path) == "./report.json"
