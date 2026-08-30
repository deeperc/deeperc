"""Structural invariants of the checker registry (Phase 2, spec §3.2).

The provenance-derivation enforcement (every steps/step_08*.py appears in the derived
CHECKER_SOURCE_FILES) lives in test_provenance_registry.py — it lands with the
provenance commit. Here we assert the registry itself is well-formed.
"""
import sys
from pathlib import Path

from steps import checker_registry as reg

POC = Path(__file__).resolve().parent.parent


def test_seven_entries_with_expected_step_ids():
    # 08f added by M2 v1 (output-conflict, VERDICT_MOVING, 2026-07-10);
    # 08g added by the merged pull-up-presence build (Todo 243+212, VERDICT_MOVING).
    assert [s.step_id for s in reg.REGISTRY] == ["08", "08b", "08c", "08d", "08e", "08f", "08g"]


def test_step_ids_names_and_buckets_are_unique():
    step_ids = [s.step_id for s in reg.REGISTRY]
    names = [s.name for s in reg.REGISTRY]
    buckets = [s.report_bucket for s in reg.REGISTRY]
    assert len(set(step_ids)) == len(step_ids)
    assert len(set(names)) == len(names)
    assert len(set(buckets)) == len(buckets), "report_bucket must be unique (classify_report keys on it)"


def test_every_source_file_exists_on_disk():
    for s in reg.REGISTRY:
        assert (POC / s.source_file).is_file(), f"{s.source_file} missing for {s.name}"


def test_verdict_role_split_matches_todays_truth():
    moving = {s.step_id for s in reg.VERDICT_MOVING}
    # 08e promoted to VERDICT_MOVING by TODO-164 (measured flip, 2026-07-07);
    # 08f (M2 output-conflict) is VERDICT_MOVING from birth (Phase 2, 2026-07-10);
    # 08g (pull-up presence, Todo 243+212) is VERDICT_MOVING from birth (WARN-tier).
    assert moving == {"08", "08b", "08c", "08d", "08e", "08f", "08g"}
    assert reg.BY_STEP["08e"].verdict_role is reg.VerdictRole.VERDICT_MOVING
    assert reg.BY_STEP["08f"].verdict_role is reg.VerdictRole.VERDICT_MOVING
    assert reg.BY_STEP["08g"].verdict_role is reg.VerdictRole.VERDICT_MOVING


def test_by_step_and_by_name_cover_the_registry():
    assert set(reg.BY_STEP) == {s.step_id for s in reg.REGISTRY}
    assert set(reg.BY_NAME) == {s.name for s in reg.REGISTRY}


def test_source_files_export_matches_registry_order():
    assert reg.SOURCE_FILES == tuple(s.source_file for s in reg.REGISTRY)


def test_status_readers_read_the_right_field():
    # signal/supply/structural read `status`; peripheral/pullup read `severity`.
    assert reg.BY_STEP["08"].extract_statuses([{"status": "fail"}]) == ["FAIL"]
    assert reg.BY_STEP["08d"].extract_statuses([{"severity": "warn"}]) == ["WARN"]
    assert reg.BY_STEP["08e"].extract_statuses([{"severity": "unresolvable"}]) == ["UNRESOLVABLE"]
    # missing field → empty-string sentinel (never crashes classify_report)
    assert reg.BY_STEP["08"].extract_statuses([{}]) == [""]


# ── Todo 248: registry-derived per-checker compare-tooling counts ─────────────────

def test_summary_key_set_for_every_verdict_moving_checker_except_signal():
    # "signal"/step_08's counts ARE the top-level summary aggregate already (pass_count
    # etc.) — every other VERDICT_MOVING checker must declare a summary_key so its
    # findings gain a corpus_baseline.py-visible compare surface.
    for spec in reg.VERDICT_MOVING:
        if spec.name == "signal":
            assert spec.summary_key is None
        else:
            assert spec.summary_key, f"{spec.name} has no summary_key — invisible to compare_baseline()"


def test_derive_checker_counts_matches_real_registry_shape():
    summary = {
        "supply_checks":           {"total": 5, "pass": 3, "warn": 1, "fail": 1, "unresolvable": 0},
        "structural_checks":       {"total": 9, "pass": 9, "warn": 0, "fail": 0},
        "peripheral_checks":       {"total": 4, "pass": 0, "warn": 2, "fail": 0, "unresolvable": 2},
        "pullup_value_checks":     {"total": 2, "warn": 1, "fail": 0, "unresolvable": 0},
        "output_conflict_checks":  {"total": 0, "fail": 0},
        "pullup_presence_checks":  {"total": 3, "warn": 3},
    }
    counts = reg.derive_checker_counts(summary)
    assert counts == {
        "supply_pass": 3, "supply_warn": 1, "supply_fail": 1, "supply_unresolvable": 0,
        "structural_pass": 9, "structural_warn": 0, "structural_fail": 0,
        "peripheral_pass": 0, "peripheral_warn": 2, "peripheral_fail": 0, "peripheral_unresolvable": 2,
        "pullup_value_warn": 1, "pullup_value_fail": 0, "pullup_value_unresolvable": 0,
        "output_conflict_fail": 0,
        "pullup_presence_warn": 3,
    }
    # structural_checks has no "unresolvable" sub-key at all (step_08c never emits
    # UNRESOLVABLE) — must not be invented/defaulted to 0.
    assert "structural_unresolvable" not in counts


def test_synthetic_checker_gains_compare_surface_with_no_corpus_baseline_edit():
    # The structural guarantee (Todo 248 design): a NEW checker spec with a summary_key
    # is fully visible to corpus_baseline.py's classification with ZERO edits to that
    # module — proven here by injecting a synthetic spec (not touching the real
    # REGISTRY) and feeding its derived keys straight into _classify_diff.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    import corpus_baseline

    synthetic = reg.CheckerSpec(
        name="synthetic_new", step_id="08z",
        source_file="steps/step_08_checker.py",  # any real file; unused by this test
        report_bucket="synthetic_new_results",
        verdict_role=reg.VerdictRole.VERDICT_MOVING,
        run=lambda ctx: [],
        summary_key="synthetic_new_checks",
    )

    summary_before = {"synthetic_new_checks": {"total": 3, "warn": 0, "fail": 0}}
    summary_after  = {"synthetic_new_checks": {"total": 3, "warn": 2, "fail": 0}}
    counts_before = reg.derive_checker_counts(summary_before, specs=(synthetic,))
    counts_after  = reg.derive_checker_counts(summary_after,  specs=(synthetic,))
    assert counts_before == {"synthetic_new_warn": 0, "synthetic_new_fail": 0}
    assert counts_after  == {"synthetic_new_warn": 2, "synthetic_new_fail": 0}

    before = corpus_baseline._extract_stats({"status": "all_pass", **counts_before})
    after  = corpus_baseline._extract_stats({"status": "has_warn", **counts_after})
    cat, deltas, residual = corpus_baseline._classify_diff(before, after)
    assert cat == "regressed"
    assert deltas["synthetic_new_warn"] == 2
    assert residual == {}, "a properly-suffixed new-checker key must be SCORED automatically"
