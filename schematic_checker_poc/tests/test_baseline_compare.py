"""Tests for corpus_baseline — save/compare logic."""
import json
import sys
from pathlib import Path

import pytest

# corpus_baseline lives at the repo root, three levels up from this file.
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import corpus_baseline
from corpus_baseline import _classify_diff, _extract_stats


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _nl(
    name: str,
    *,
    status: str = "all_pass",
    sp: int = 0, sw: int = 0, sf: int = 0,   # structural P/W/F
    pp: int = 0, pw: int = 0, pf: int = 0,   # supply P/W/F
    rp: int = 0, rw: int = 0, rf: int = 0,   # peRipheral P/W/F
    p:  int = 0, w:  int = 0, f:  int = 0,   # signal P/W/F
    u:  int = 0,                              # signal unresolvable
) -> dict:
    return {
        "netlist": name,
        "status":  status,
        "structural_pass":    sp, "structural_warn":    sw, "structural_fail":    sf,
        "supply_pass":        pp, "supply_warn":        pw, "supply_fail":        pf,
        "peripheral_pass":    rp, "peripheral_warn":    rw, "peripheral_fail":    rf,
        "pass_count":         p,  "warn_count":         w,  "fail_count":         f,
        "unresolvable_count": u,
    }


def _summary(netlists: list[dict], sha: str = "abc1234") -> dict:
    return {
        "generated_at": "2026-05-11T00:00:00+00:00",
        "git_sha":      sha,
        "per_netlist":  netlists,
    }


def _write_summary(path: Path, netlists: list[dict], sha: str = "abc1234") -> None:
    path.write_text(json.dumps(_summary(netlists, sha=sha)))


def _run_compare(tmp_path: Path, baseline_netlists, current_netlists) -> dict:
    base_path = tmp_path / "baseline.json"
    cur_path  = tmp_path / "current.json"
    _write_summary(base_path, baseline_netlists, sha="aaa0000")
    _write_summary(cur_path,  current_netlists,  sha="bbb1111")
    corpus_baseline.compare_baseline(cur_path, base_path, tmp_path)
    return json.loads((tmp_path / "baseline_comparison.json").read_text())


# ── Test 1: Identical runs produce zero diffs ─────────────────────────────────

def test_identical_unchanged(tmp_path):
    netlists = [
        _nl("foo/a.net", status="all_pass", sp=5, sw=0, sf=0),
        _nl("foo/b.net", status="has_warn", sp=3, sw=1, sf=0),
        _nl("foo/c.net", status="all_pass", sp=2, sw=0, sf=0, pp=1),
    ]
    result = _run_compare(tmp_path, netlists, netlists)

    assert result["counts"]["unchanged"] == 3
    assert result["counts"]["improved"]  == 0
    assert result["counts"]["regressed"] == 0
    assert result["counts"]["mixed"]     == 0
    assert result["counts"]["new"]       == 0
    assert result["counts"]["removed"]   == 0
    assert result["regressions"] == []
    assert result["improvements"] == []


# ── Test 2: FAIL→PASS shows as improvement ────────────────────────────────────

def test_fail_to_pass_improved(tmp_path):
    base = [_nl("foo/bar.net", status="has_fail", sp=0, sf=1)]
    cur  = [_nl("foo/bar.net", status="all_pass", sp=1, sf=0)]

    result = _run_compare(tmp_path, base, cur)

    assert result["counts"]["improved"]  == 1
    assert result["counts"]["regressed"] == 0
    assert len(result["improvements"]) == 1
    entry = result["improvements"][0]
    assert entry["netlist"] == "foo/bar.net"
    assert entry["deltas"]["structural_pass"] == 1
    assert entry["deltas"]["structural_fail"] == -1


# ── Test 3: PASS→FAIL shows as regression; returns True (exit code 1) ─────────

def test_pass_to_fail_regressed(tmp_path):
    base = [_nl("foo/bar.net", status="all_pass", sp=5, sf=0)]
    cur  = [_nl("foo/bar.net", status="has_fail", sp=4, sf=1)]

    base_path = tmp_path / "baseline.json"
    cur_path  = tmp_path / "current.json"
    _write_summary(base_path, base)
    _write_summary(cur_path,  cur)

    has_regressions = corpus_baseline.compare_baseline(cur_path, base_path, tmp_path)

    assert has_regressions is True
    result = json.loads((tmp_path / "baseline_comparison.json").read_text())
    assert result["counts"]["regressed"] == 1
    assert result["counts"]["improved"]  == 0
    assert len(result["regressions"]) == 1
    entry = result["regressions"][0]
    assert entry["deltas"]["structural_fail"] == 1
    assert entry["deltas"]["structural_pass"] == -1


# ── Test 4: New netlist in current → "new" category ──────────────────────────

def test_new_netlist(tmp_path):
    base = [_nl("foo/old.net", sp=3)]
    cur  = [_nl("foo/old.net", sp=3), _nl("foo/new.net", sp=2)]

    result = _run_compare(tmp_path, base, cur)

    assert result["counts"]["new"]       == 1
    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["improved"]  == 0
    assert result["counts"]["regressed"] == 0
    assert "foo/new.net" in result["new"]


# ── Test 5: Mixed — one WARN→PASS and one new FAIL on same netlist ────────────

def test_mixed_case(tmp_path):
    # structural: sw 1→0 (improvement), sf 0→1 (regression) on the same netlist
    base = [_nl("foo/mixed.net", status="has_warn", sp=5, sw=1, sf=0)]
    cur  = [_nl("foo/mixed.net", status="has_fail", sp=5, sw=0, sf=1)]

    base_path = tmp_path / "baseline.json"
    cur_path  = tmp_path / "current.json"
    _write_summary(base_path, base)
    _write_summary(cur_path,  cur)

    has_regressions = corpus_baseline.compare_baseline(cur_path, base_path, tmp_path)

    assert has_regressions is True  # mixed counts as needing attention
    result = json.loads((tmp_path / "baseline_comparison.json").read_text())
    assert result["counts"]["mixed"]     == 1
    assert result["counts"]["regressed"] == 0
    assert result["counts"]["improved"]  == 0
    assert len(result["mixed"]) == 1
    entry = result["mixed"][0]
    assert entry["deltas"]["structural_warn"] == -1
    assert entry["deltas"]["structural_fail"] == 1


# ── Test 6: UNRESOLVABLE→PASS with matching pass delta → improvement ──────────

def test_unresolvable_to_pass_is_improvement(tmp_path):
    # A net that was UNRESOLVABLE is now PASS: u -1, p +1.
    base = [_nl("foo/bar.net", u=3, p=0)]
    cur  = [_nl("foo/bar.net", u=2, p=1)]

    result = _run_compare(tmp_path, base, cur)

    assert result["counts"]["improved"]  == 1
    assert result["counts"]["regressed"] == 0

    # Edge case: unresolvable count increased but pass increased by same amount
    # (new unresolvable appeared, but existing ones resolved) → NOT regressed.
    base2 = [_nl("foo/edge.net", u=1, p=0)]
    cur2  = [_nl("foo/edge.net", u=2, p=1)]  # +1 unres, +1 pass: net unres +1 covered by pass +1

    cat, deltas, residual = _classify_diff(_extract_stats(base2[0]), _extract_stats(cur2[0]))
    # pass_count increased (+1) >= unres_delta (+1) → should not flag as regression
    assert cat != "regressed"


# ── Test 7: Dirty working tree appends -dirty to baseline filename ─────────────

def test_dirty_tree_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus_baseline, "git_sha",   lambda cwd=None: "abc1234")
    monkeypatch.setattr(corpus_baseline, "git_dirty", lambda cwd=None: True)

    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"per_netlist": [], "generated_at": "2026-05-11"}))
    baselines_dir = tmp_path / "baselines"

    saved = corpus_baseline.save_baseline(summary_path, baselines_dir)

    assert "-dirty" in saved.name
    assert "abc1234" in saved.name
    assert saved.exists()
    assert (baselines_dir / "latest.json").exists()


# ── Test 7b: Clean working tree → no -dirty suffix ────────────────────────────

def test_clean_tree_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus_baseline, "git_sha",   lambda cwd=None: "def5678")
    monkeypatch.setattr(corpus_baseline, "git_dirty", lambda cwd=None: False)

    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"per_netlist": []}))
    baselines_dir = tmp_path / "baselines"

    saved = corpus_baseline.save_baseline(summary_path, baselines_dir)

    assert "-dirty" not in saved.name
    assert "def5678" in saved.name


# ── Test: resolve_baseline_path "latest" alias ────────────────────────────────

def test_resolve_latest(tmp_path):
    output_dir = tmp_path / "corpus_results"
    p = corpus_baseline.resolve_baseline_path("latest", output_dir)
    assert p == output_dir / "baselines" / "latest.json"


def test_resolve_explicit_path(tmp_path):
    explicit = tmp_path / "mybaseline.json"
    p = corpus_baseline.resolve_baseline_path(str(explicit), tmp_path)
    assert p == explicit


# ── Test: removed netlist → "removed" category ───────────────────────────────

def test_removed_netlist(tmp_path):
    base = [_nl("foo/old.net", sp=3), _nl("foo/gone.net", sp=1)]
    cur  = [_nl("foo/old.net", sp=3)]

    result = _run_compare(tmp_path, base, cur)

    assert result["counts"]["removed"]   == 1
    assert result["counts"]["unchanged"] == 1
    assert "foo/gone.net" in result["removed"]


# ── Peripheral-fail scoring (M6 coherence gate honesty, commit follows) ───────

def test_peripheral_fail_only_is_regressed(tmp_path):
    # Load-bearing: a NEW peripheral FAIL on an otherwise-unchanged board must be
    # scored as a regression. Before the fix, peripheral_fail was unscored and
    # this board landed in `unchanged` — the gate was blind to it.
    base = [_nl("foo/swap.net", status="all_pass", rf=0)]
    cur  = [_nl("foo/swap.net", status="has_fail", rf=2)]

    base_path = tmp_path / "baseline.json"
    cur_path  = tmp_path / "current.json"
    _write_summary(base_path, base)
    _write_summary(cur_path,  cur)

    has_regressions = corpus_baseline.compare_baseline(cur_path, base_path, tmp_path)

    assert has_regressions is True              # exit 1
    result = json.loads((tmp_path / "baseline_comparison.json").read_text())
    assert result["counts"]["regressed"] == 1
    assert result["counts"]["improved"]  == 0
    assert result["regressions"][0]["deltas"]["peripheral_fail"] == 2


def test_peripheral_fail_with_cooccurring_improvement_is_mixed(tmp_path):
    # The real jetson U54 case: a new peripheral FAIL (regression) co-occurs with
    # a cache-drift structural_pass gain (improvement) on the SAME board. Faithful
    # to the existing multi-axis semantics this is `mixed`, NOT `improved` (where
    # it used to hide) and not pure `regressed`. The load-bearing assertion is the
    # exit signal: mixed must still return True so the gate fails.
    base = [_nl("jetson/peripherals.net", status="all_pass", sp=10, rf=0)]
    cur  = [_nl("jetson/peripherals.net", status="has_fail", sp=14, rf=2)]

    base_path = tmp_path / "baseline.json"
    cur_path  = tmp_path / "current.json"
    _write_summary(base_path, base)
    _write_summary(cur_path,  cur)

    has_regressions = corpus_baseline.compare_baseline(cur_path, base_path, tmp_path)

    assert has_regressions is True              # exit 1 — load-bearing
    result = json.loads((tmp_path / "baseline_comparison.json").read_text())
    assert result["counts"]["mixed"]     == 1
    assert result["counts"]["improved"]  == 0   # no longer hidden here
    assert result["counts"]["regressed"] == 0
    entry = result["mixed"][0]
    assert entry["deltas"]["peripheral_fail"]  == 2
    assert entry["deltas"]["structural_pass"]  == 4


def test_peripheral_fail_removed_is_improvement(tmp_path):
    # Symmetry: a REMOVED peripheral FAIL scores as an improvement.
    base = [_nl("foo/fixed.net", status="has_fail", rf=2)]
    cur  = [_nl("foo/fixed.net", status="all_pass", rf=0)]

    result = _run_compare(tmp_path, base, cur)

    assert result["counts"]["improved"]  == 1
    assert result["counts"]["regressed"] == 0
    assert result["improvements"][0]["deltas"]["peripheral_fail"] == -2


def test_peripheral_warn_increase_is_regressed(tmp_path):
    # peripheral_warn mirrors structural_warn/supply_warn: an increase regresses.
    base = [_nl("foo/w.net", status="all_pass", rw=0)]
    cur  = [_nl("foo/w.net", status="has_warn", rw=1)]

    result = _run_compare(tmp_path, base, cur)
    assert result["counts"]["regressed"] == 1
    assert result["counts"]["improved"]  == 0


# ── Folded-in deferred fix: pipeline_error baseline is not scored ─────────────

def test_pipeline_error_baseline_is_unchanged(tmp_path):
    # A board that previously errored out (0 checks) now running with FAILs is the
    # board becoming evaluable, not a regression measured off the baseline's zeros.
    # (CLAUDE.md deferred _classify_diff note, folded into this commit.)
    base = [_nl("foo/crashed.net", status="pipeline_error")]
    cur  = [_nl("foo/crashed.net", status="has_fail", sf=1, rf=1, f=3)]

    result = _run_compare(tmp_path, base, cur)

    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["regressed"] == 0
    assert result["counts"]["mixed"]     == 0
    assert result["regressions"] == []


# ── Gap-closer: EVERY verdict-moving dimension must move the diff (ID-95 class) ─
# One board, one dimension gaining a WARN, status worsening all_pass → has_warn. Each
# of the five VERDICT_MOVING checkers must be scored — the granular ones via their key,
# and step_08e pullup (which run_checks does NOT emit a granular count for) via the
# registry-derived verdict-status transition. This test fails by construction if any
# verdict-moving dimension is unscored: it would have caught BOTH the ID-95 original
# (peripheral_fail unscored) and the TODO-164 pullup blind spot (regressed:0 despite the
# jetson recovery_usb all_pass → has_warn move).
_VERDICT_DIMENSIONS = [
    ("structural", {"sw": 1}),   # step_08c → structural_warn
    ("supply",     {"pw": 1}),   # step_08b → supply_warn
    ("peripheral", {"rw": 1}),   # step_08d → peripheral_warn
    ("signal",     {"w":  1}),   # step_08  → warn_count
    ("pullup",     {}),          # step_08e → NO granular key; caught via status transition
]


@pytest.mark.parametrize("label,warn_kwargs", _VERDICT_DIMENSIONS)
def test_each_verdict_dimension_regresses(tmp_path, label, warn_kwargs):
    base = [_nl("foo/dim.net", status="all_pass", sp=1)]
    cur  = [_nl("foo/dim.net", status="has_warn", sp=1, **warn_kwargs)]
    result = _run_compare(tmp_path, base, cur)
    assert result["counts"]["regressed"] == 1, f"{label} dimension not scored as a regression"
    assert result["counts"]["improved"]  == 0


@pytest.mark.parametrize("label,warn_kwargs", _VERDICT_DIMENSIONS)
def test_each_verdict_dimension_improves_inverse(tmp_path, label, warn_kwargs):
    # Symmetry: the same WARN removed (has_warn → all_pass) scores as an improvement.
    base = [_nl("foo/dim.net", status="has_warn", sp=1, **warn_kwargs)]
    cur  = [_nl("foo/dim.net", status="all_pass", sp=1)]
    result = _run_compare(tmp_path, base, cur)
    assert result["counts"]["improved"]  == 1, f"{label} dimension inverse not scored as an improvement"
    assert result["counts"]["regressed"] == 0


# ── Todo 248 (compare_staleness_recon fix): peripheral_unresolvable / supply_unresolvable
# now consulted; real dcdc.net records (run_096_20260715_140809.json vs
# baseline_3f35408-dirty_20260714.json, both retained on disk) reproduced verbatim ─────

_DCDC_BEFORE = {
    "netlist": "exported/kicad_official/demos/demos/jetson-agx-thor-baseboard/dcdc.net",
    "status": "has_warn",
    "fail_count": 0, "warn_count": 0, "pass_count": 14, "unresolvable_count": 25,
    "nets_checked": 39,
    "supply_pass": 0, "supply_warn": 0, "supply_fail": 0, "supply_unresolvable": 0,
    "structural_pass": 9, "structural_warn": 0, "structural_fail": 0,
    "peripheral_pass": 0, "peripheral_warn": 2, "peripheral_fail": 0,
    "peripheral_unresolvable": 2,
}
_DCDC_AFTER = {**_DCDC_BEFORE, "peripheral_unresolvable": 0}


def test_real_dcdc_record_no_longer_silently_unchanged():
    # Direct _classify_diff repro (the exact call the recon report used) — must NOT be
    # "unchanged" now that peripheral_unresolvable has polarity.
    cat, deltas, residual = _classify_diff(
        _extract_stats(_DCDC_BEFORE), _extract_stats(_DCDC_AFTER))
    assert cat == "improved"
    assert deltas["peripheral_unresolvable"] == -2
    assert residual == {}, "peripheral_unresolvable must be SCORED, not residual"


def test_real_dcdc_record_via_compare_baseline(tmp_path):
    result = _run_compare(tmp_path, [_DCDC_BEFORE], [_DCDC_AFTER])
    assert result["counts"]["improved"]  == 1
    assert result["counts"]["unchanged"] == 0
    entry = result["improvements"][0]
    assert entry["deltas"]["peripheral_unresolvable"] == -2
    # Tagged distinctly as a coverage movement, not folded into verdict-severity.
    assert entry["coverage_deltas"] == {"peripheral_unresolvable": -2}


# ── Todo 248: registry-derived per-checker keys make step_08g findings visible ────
# Real Phase-B same-status trio (PHASEB_LANDING.md §1): all three previously classified
# "unchanged" (no granular key existed for pullup_presence at all).

def test_phaseb_trio_regresses_via_derived_pullup_presence_key():
    cases = [
        ("ESP32-EVB_Rev_L.net",             "has_warn", "has_warn", 3),
        ("ESP32-PoE2_Rev_B.net",            "has_warn", "has_warn", 4),
        ("jetson-agx-thor-baseboard.net",   "has_fail", "has_fail", 2),
    ]
    for netlist, before_status, after_status, warn_delta in cases:
        before = {"netlist": netlist, "status": before_status, "pullup_presence_warn": 0}
        after  = {"netlist": netlist, "status": after_status,  "pullup_presence_warn": warn_delta}
        cat, deltas, residual = _classify_diff(_extract_stats(before), _extract_stats(after))
        assert cat == "regressed", f"{netlist}: same-status +{warn_delta} pullup_presence WARNs not scored"
        assert deltas["pullup_presence_warn"] == warn_delta
        assert residual == {}


# ── Todo 248: residual-delta invariant — unscored key survives, never silently dropped ─

def test_residual_delta_invariant_unscored_key_surfaces(tmp_path):
    # "retry_count" ends in "_count" (so _extract_stats retains it, like the four base
    # aggregates) but is not one of the four recognized base keys and matches no
    # _pass/_warn/_fail/_unresolvable suffix — deliberately outside every classification
    # list. The netlist must still classify "unchanged" (nothing else moved) but the
    # delta must NOT vanish: it belongs in residual_deltas, with a console warning.
    base = [{"netlist": "foo/odd.net", "status": "all_pass", "retry_count": 1}]
    cur  = [{"netlist": "foo/odd.net", "status": "all_pass", "retry_count": 4}]

    cat, deltas, residual = _classify_diff(_extract_stats(base[0]), _extract_stats(cur[0]))
    assert cat == "unchanged"
    assert deltas["retry_count"] == 3
    assert residual == {"retry_count": 3}

    result = _run_compare(tmp_path, base, cur)
    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["regressed"] == 0
    assert result["counts"]["improved"]  == 0
    assert len(result["residual_deltas"]) == 1
    assert result["residual_deltas"][0]["netlist"] == "foo/odd.net"
    assert result["residual_deltas"][0]["residual_deltas"] == {"retry_count": 3}


def test_residual_delta_invariant_empty_when_nothing_unscored(tmp_path):
    netlists = [_nl("foo/clean.net", status="all_pass", sp=5)]
    result = _run_compare(tmp_path, netlists, netlists)
    assert result["residual_deltas"] == []


# ── Todo 248: retention — timestamped comparison artifacts, compat copy preserved ─────

def test_compare_writes_timestamped_and_legacy_copies(tmp_path):
    base = [_nl("foo/a.net", status="all_pass", sp=1)]
    cur  = [_nl("foo/a.net", status="has_warn", sp=1, sw=1)]

    base_path = tmp_path / "baseline.json"
    cur_path  = tmp_path / "current.json"
    _write_summary(base_path, base, sha="aaa0000")
    _write_summary(cur_path,  cur,  sha="bbb1111")

    corpus_baseline.compare_baseline(cur_path, base_path, tmp_path)

    legacy = tmp_path / "baseline_comparison.json"
    assert legacy.exists()

    comparisons = sorted((tmp_path / "comparisons").glob("compare_*.json"))
    assert len(comparisons) == 1
    assert json.loads(comparisons[0].read_text()) == json.loads(legacy.read_text())


def test_two_consecutive_compares_do_not_overwrite(tmp_path, monkeypatch):
    base = [_nl("foo/a.net", status="all_pass", sp=1)]
    cur1 = [_nl("foo/a.net", status="has_warn", sp=1, sw=1)]
    cur2 = [_nl("foo/a.net", status="has_fail", sp=1, sf=1)]

    base_path = tmp_path / "baseline.json"
    cur1_path = tmp_path / "current1.json"
    cur2_path = tmp_path / "current2.json"
    _write_summary(base_path, base, sha="aaa0000")

    def _summary_with_ts(netlists, sha, ts):
        return {"generated_at": ts, "git_sha": sha, "per_netlist": netlists}

    cur1_path.write_text(json.dumps(_summary_with_ts(cur1, "bbb1111", "2026-07-16T10:00:00+00:00")))
    cur2_path.write_text(json.dumps(_summary_with_ts(cur2, "ccc2222", "2026-07-16T11:00:00+00:00")))

    corpus_baseline.compare_baseline(cur1_path, base_path, tmp_path)
    corpus_baseline.compare_baseline(cur2_path, base_path, tmp_path)

    comparisons = sorted((tmp_path / "comparisons").glob("compare_*.json"))
    assert len(comparisons) == 2, "second compare must not overwrite the first's artifact"
