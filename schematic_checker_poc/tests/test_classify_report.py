"""Fixture-matrix cross-check: pipeline.classify_report (array-based) vs an
independent summary-based reimplementation, across every precedence branch.

`_legacy_classify` below started as a VERBATIM copy of the pre-relocation
run_corpus_test.classify_netlist_status (git-blame anchor: run_corpus_test.py @
2e14de8) — that Phase-2 copy proved the registry refactor preserved behavior
byte-for-byte. It now also counts the 08e pullup bucket, which joined the
VERDICT_MOVING set in TODO-164 (2026-07-07); the two implementations read
different report surfaces (result ARRAYS vs summary sub-dicts), so their agreement
proves classify_report matches the current verdict contract — not merely my
expectations.
"""
import sys
from pathlib import Path

import pytest

POC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(POC))

from pipeline import classify_report


# ── verbatim legacy function (summary-based) ────────────────────────────────────
def _legacy_classify(report: dict) -> str:
    if report.get("pipeline_error"):
        return "pipeline_error"
    summary = report.get("summary", {})
    net_total = summary.get("total_nets_checked", 0)
    supply_summary = summary.get("supply_checks", {})
    supply_total = supply_summary.get("total", 0)
    struct_summary = summary.get("structural_checks", {})
    struct_total = struct_summary.get("total", 0)
    periph_summary = summary.get("peripheral_checks", {})
    periph_total = periph_summary.get("total", 0)
    # 08e pullup-value joined the VERDICT_MOVING set in TODO-164 (2026-07-07); this
    # summary-based cross-check tracks the registry, so it reads pullup counts too.
    pullup_summary = summary.get("pullup_value_checks", {})
    pullup_total = pullup_summary.get("total", 0)
    if (net_total == 0 and supply_total == 0 and struct_total == 0
            and periph_total == 0 and pullup_total == 0):
        return "no_checkable_nets"
    fails = (summary.get("fail", 0) + supply_summary.get("fail", 0)
             + struct_summary.get("fail", 0) + periph_summary.get("fail", 0)
             + pullup_summary.get("fail", 0))
    warns = (summary.get("warn", 0) + supply_summary.get("warn", 0)
             + struct_summary.get("warn", 0) + periph_summary.get("warn", 0)
             + pullup_summary.get("warn", 0))
    passes = (summary.get("pass", 0) + supply_summary.get("pass", 0)
              + struct_summary.get("pass", 0) + periph_summary.get("pass", 0))
    unres = (summary.get("unresolvable", 0) + supply_summary.get("unresolvable", 0)
             + periph_summary.get("unresolvable", 0)
             + pullup_summary.get("unresolvable", 0))
    if fails > 0:
        return "has_fail"
    if warns > 0:
        return "has_warn"
    if passes > 0:
        return "all_pass"
    if unres > 0:
        return "has_unresolvable_only"
    return "no_checkable_nets"


# ── fixture builder: arrays + summary, consistent ───────────────────────────────
def _arr(counts, field):
    """List of {field: STATUS} for (pass, warn, fail, unres) counts (drop labels
    the bucket doesn't carry by passing 0)."""
    p, w, f, u = counts
    out = []
    out += [{field: "PASS"}] * p
    out += [{field: "WARN"}] * w
    out += [{field: "FAIL"}] * f
    out += [{field: "UNRESOLVABLE"}] * u
    return out


def _mk(net=(0, 0, 0, 0), sup=(0, 0, 0, 0), st=(0, 0, 0), per=(0, 0, 0, 0), pull=(0, 0, 0)):
    """Build a report with mutually-consistent result arrays + summary block.
    st has no unresolvable dimension; pull (08e, VERDICT_MOVING since TODO-164) is (warn, fail, unres)."""
    net_a = _arr(net, "status")
    sup_a = _arr(sup, "status")
    st_a = _arr((st[0], st[1], st[2], 0), "status")
    per_a = _arr(per, "severity")
    pull_a = _arr((0, pull[0], pull[1], pull[2]), "severity")
    return {
        "summary": {
            "total_nets_checked": len(net_a),
            "pass": net[0], "warn": net[1], "fail": net[2], "unresolvable": net[3],
            "supply_checks": {"total": len(sup_a), "pass": sup[0], "warn": sup[1],
                              "fail": sup[2], "unresolvable": sup[3]},
            "structural_checks": {"total": len(st_a), "pass": st[0], "warn": st[1], "fail": st[2]},
            "peripheral_checks": {"total": len(per_a), "pass": per[0], "warn": per[1],
                                  "fail": per[2], "unresolvable": per[3]},
            "pullup_value_checks": {"total": len(pull_a), "warn": pull[0], "fail": pull[1],
                                    "unresolvable": pull[2]},
        },
        "results": net_a,
        "power_supply_results": sup_a,
        "structural_integrity_results": st_a,
        "peripheral_integrity_results": per_a,
        "pullup_value_results": pull_a,
    }


# (label, report) — every branch + precedence interaction + 08e (VERDICT_MOVING) participation.
CASES = [
    ("pipeline_error",         {"pipeline_error": "boom"}),
    ("no_checkable_nets",      _mk()),
    ("has_fail",               _mk(net=(0, 0, 1, 0))),                 # signal FAIL
    ("has_fail",               _mk(sup=(0, 0, 1, 0))),                 # supply FAIL only
    ("has_fail",               _mk(st=(0, 0, 1))),                     # structural FAIL only
    ("has_fail",               _mk(per=(0, 0, 1, 0))),                 # peripheral FAIL only
    ("has_warn",               _mk(net=(0, 1, 0, 0))),                 # signal WARN
    ("has_warn",               _mk(st=(0, 1, 0))),                     # structural WARN only
    ("all_pass",               _mk(net=(1, 0, 0, 0))),                 # signal PASS
    ("all_pass",               _mk(st=(1, 0, 0))),                     # structural PASS only
    ("has_unresolvable_only",  _mk(net=(0, 0, 0, 1))),                 # signal UNRES only
    ("has_unresolvable_only",  _mk(sup=(0, 0, 0, 1))),                 # supply UNRES only
    ("has_unresolvable_only",  _mk(per=(0, 0, 0, 1))),                 # peripheral UNRES only
    ("all_pass",               _mk(net=(1, 0, 0, 1))),                 # PASS+UNRES → all_pass (real precedence)
    ("has_warn",               _mk(net=(0, 1, 0, 1))),                 # WARN+UNRES → has_warn
    ("has_fail",               _mk(net=(1, 1, 1, 1))),                 # everything → has_fail
    ("has_warn",               _mk(net=(1, 1, 0, 0))),                 # PASS+WARN → has_warn
    ("has_fail",               _mk(pull=(0, 1, 0))),                   # 08e FAIL only (now VERDICT_MOVING, TODO-164) → has_fail
    ("has_fail",               _mk(net=(1, 0, 0, 0), pull=(0, 1, 0))),  # 08e FAIL + signal PASS → has_fail (08e now moves verdict)
    ("has_warn",               _mk(net=(1, 0, 0, 0), pull=(1, 0, 0))),  # 08e WARN + signal PASS → has_warn (the recovery_usb case)
]


@pytest.mark.parametrize("expected,report", CASES)
def test_classify_report_matches_expected(expected, report):
    assert classify_report(report) == expected


@pytest.mark.parametrize("expected,report", CASES)
def test_classify_report_reproduces_legacy(expected, report):
    assert classify_report(report) == _legacy_classify(report)


def test_buckets_present_but_status_unrecognized_falls_through():
    # A result with a status neither function counts: legacy sees a non-zero total
    # but zero P/W/F/U → no_checkable_nets; classify_report sees a non-empty status
    # list with zero recognized counts → the same fallback. They must agree.
    report = {
        "summary": {"total_nets_checked": 1, "pass": 0, "warn": 0, "fail": 0, "unresolvable": 0,
                    "supply_checks": {"total": 0}, "structural_checks": {"total": 0},
                    "peripheral_checks": {"total": 0}, "pullup_value_checks": {"total": 0}},
        "results": [{"status": "SKIPPED"}],
        "power_supply_results": [], "structural_integrity_results": [],
        "peripheral_integrity_results": [], "pullup_value_results": [],
    }
    assert classify_report(report) == "no_checkable_nets"
    assert classify_report(report) == _legacy_classify(report)
