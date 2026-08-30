"""F6 / TODO-173 — tiered recall transparency: sub-cause → tier classification.

These lock the CORRECTNESS RULE that makes the tiering honest: the coverage-gap
rate denominator is assembled from GENUINE-GAP SUB-CAUSE labels, never from whole
top-level buckets. UNEVALUABLE is composite (generator_degenerate [Tier D] +
coverage_gap/export_classifier_gap [Tier B] + fundamental_hw_kb [Tier C]), so
folding it whole would mis-penalize the detector — the exact error the tiering
exists to prevent (recon TODO-173).

Pure unit tests over compute_operator_tiers / aggregate_tiers — synthetic row
dicts, no I/O, no re-run.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo root

rrh = pytest.importorskip("run_recall_harness")


def _rows(spec):
    """spec: list of (bucket, subcause, count) → flat list of row dicts."""
    out = []
    for bucket, subcause, n in spec:
        for _ in range(n):
            r = {"bucket": bucket}
            if subcause is not None:
                r["unevaluable_subcause"] = subcause
            out.append(r)
    return out


def test_composite_unevaluable_folds_only_coverage_gap_not_generator_degenerate():
    """M1-shaped: coverage-gap rate folds coverage_gap (Tier B) but NOT
    generator_degenerate (Tier D), even though both sit under UNEVALUABLE."""
    rows = _rows([
        ("HIT", None, 7), ("MISS", None, 11), ("UNRESOLVABLE", None, 14),
        ("UNEVALUABLE", "generator_degenerate", 7),   # Tier D — must NOT penalize
        ("UNEVALUABLE", "coverage_gap", 6),           # Tier B — must penalize
    ])
    t = rrh.compute_operator_tiers(rows)
    assert t["evaluable"] == 18
    assert t["evaluable_rate_pct"] == pytest.approx(100 * 7 / 18)
    # Tier B denom = HIT+MISS + UNRESOLVABLE(14) + coverage_gap(6) = 38 — the 7
    # generator_degenerate are EXCLUDED. A naive fold-all-UNEVALUABLE would be 45.
    assert t["coverage_gap_denominator"] == 38
    assert t["coverage_gap_denominator"] != 45, "generator_degenerate must not be folded in"
    assert t["coverage_gap_rate_pct"] == pytest.approx(100 * 7 / 38)
    assert t["generator_artifact"] == 7          # Tier D count, in NO rate
    assert t["excluded_by_design"] == 0


def test_all_fundamental_hw_kb_is_tier_c_in_neither_rate():
    """M3-shaped: 24 fundamental_hw_kb are excluded-by-design (Tier C) — the
    evaluable AND coverage-gap rates are both over the real 2-mutant evaluable set,
    never penalized by the 24."""
    rows = _rows([
        ("HIT", None, 2), ("MISS", None, 0),
        ("UNEVALUABLE", "fundamental_hw_kb", 24),
    ])
    t = rrh.compute_operator_tiers(rows)
    assert t["evaluable"] == 2
    assert t["evaluable_rate_pct"] == pytest.approx(100.0)
    # fundamental_hw_kb folds into NEITHER rate: both denominators stay 2, not 26.
    assert t["coverage_gap_denominator"] == 2
    assert t["coverage_gap_rate_pct"] == pytest.approx(100.0)
    assert t["excluded_by_design"] == 24          # Tier C count
    assert t["generator_artifact"] == 0


def test_probe_failed_and_generator_degenerate_are_tier_d_never_in_a_rate():
    """probe_failed (harness crash) and generator_degenerate (non-defect) are
    Tier-D artifacts — counted, but in NEITHER rate's denominator."""
    rows = _rows([
        ("HIT", None, 5), ("MISS", None, 5),
        ("PROBE_FAILED", None, 3),
        ("UNEVALUABLE", "generator_degenerate", 2),
    ])
    t = rrh.compute_operator_tiers(rows)
    assert t["evaluable"] == 10
    assert t["evaluable_rate_pct"] == pytest.approx(50.0)
    # neither probe_failed nor generator_degenerate enters the coverage-gap denom.
    assert t["coverage_gap_denominator"] == 10
    assert t["coverage_gap_rate_pct"] == pytest.approx(50.0)
    assert t["probe_failed"] == 3                 # Tier D (harness)
    assert t["generator_artifact"] == 2           # Tier D (generator)


def test_export_classifier_gap_folds_into_coverage_gap():
    """M7-shaped: export_classifier_gap IS a Tier-B fixable gap — it folds into
    the coverage-gap denominator (unlike the Tier-C/D UNEVALUABLE sub-causes)."""
    rows = _rows([
        ("HIT", None, 43), ("MISS", None, 29),
        ("UNEVALUABLE", "export_classifier_gap", 5),
    ])
    t = rrh.compute_operator_tiers(rows)
    assert t["evaluable"] == 72
    assert t["coverage_gap_denominator"] == 77    # 72 + 5 export_classifier_gap
    assert t["coverage_gap_rate_pct"] == pytest.approx(100 * 43 / 77)
    assert t["excluded_by_design"] == 0
    assert t["generator_artifact"] == 0


def test_all_tier_c_operator_has_no_penalized_rate():
    """M5-shaped: an all-UNDETECTABLE_BY_DESIGN operator has 0 evaluable → both
    rates are None (never a penalized 0%), and all 4 are a Tier-C count."""
    rows = _rows([("UNDETECTABLE_BY_DESIGN", None, 4)])
    t = rrh.compute_operator_tiers(rows)
    assert t["evaluable"] == 0
    assert t["evaluable_rate_pct"] is None
    assert t["coverage_gap_rate_pct"] is None
    assert t["coverage_gap_denominator"] == 0
    assert t["excluded_by_design"] == 4


def test_aggregate_is_rate_of_sums_not_mean_of_rates():
    """Overall tiers recompute the rates over summed components (rate-of-sums)."""
    a = rrh.compute_operator_tiers(_rows([("HIT", None, 7), ("MISS", None, 11),
                                          ("UNRESOLVABLE", None, 14),
                                          ("UNEVALUABLE", "generator_degenerate", 7),
                                          ("UNEVALUABLE", "coverage_gap", 6)]))
    b = rrh.compute_operator_tiers(_rows([("HIT", None, 2),
                                          ("UNEVALUABLE", "fundamental_hw_kb", 24)]))
    ov = rrh.aggregate_tiers([a, b])
    assert ov["HIT"] == 9 and ov["evaluable"] == 20
    assert ov["evaluable_rate_pct"] == pytest.approx(100 * 9 / 20)
    # coverage-gap denom = 20 evaluable + (14 UNRESOLVABLE + 6 coverage_gap) = 40;
    # the 7 generator_degenerate (D) and 24 fundamental_hw_kb (C) are NOT in it.
    assert ov["coverage_gap_denominator"] == 40
    assert ov["coverage_gap_rate_pct"] == pytest.approx(100 * 9 / 40)
    assert ov["excluded_by_design"] == 24
    assert ov["generator_artifact"] == 7
