"""TODO-368 Phase 2 FIX — evidence-tier vocabulary + plumbing.

A SEPARATE axis from `test_doc_identity_qualifier.py`'s doc-identity suffix
qualifier (STRONG/WEAK/MISS/unverifiable — "does the cache stem appear in the
PDF text"). This layer answers "is the cache we read still current relative to
the PDF on disk" (steps.step_03_resolver.check_cache_provenance's
current/stale/legacy_unverified/current_no_pdf_verify vocabulary), via a
FROZEN prefix-rewrite + parenthetical-append. Both axes are text-only,
VERDICT-NEUTRAL, and coexist on one evidence_label: tier transform first,
doc_identity qualify second (its suffix appends to the tier-rewritten label).

See investigation/experiments/todo368_label_consumer_survey/report.md for the
recon this fix is built on (7 emission sites, 4/7 qualified buckets).
"""
import json
import logging

import pytest

from steps import doc_identity
from steps import step_10_report
from steps.step_08_checker import CheckResult
from steps.step_08b_supply_checker import SupplyCheckResult
from steps.step_08c_structural_checker import StructuralCheckResult
from steps.step_08d_peripheral_checker import (
    PeripheralFinding, PeripheralViolation, Severity,
)
from steps.step_08e_pullup_value_checker import PullupValueFinding, PullupBand
from steps.step_08g_pullup_presence import PullupPresenceFinding
from steps.m2_output_conflict import OutputConflictFinding


LABEL = "Confirmed — §5.2 Electrical Characteristics"
ASSUMED_LABEL = (
    "Assumed — pull-up R1 on I2C net SDA has value '4.7k' that could not be "
    "parsed precisely; treat as approximate"
)
FREEFORM_LABEL = "Supply spec not extracted from datasheet"

STRONG = {"class": "STRONG", "tier": 1, "depth": 0, "ratio": 1.0}
MISS = {"class": "MISS", "tier": None, "depth": None, "ratio": None}


@pytest.fixture(autouse=True)
def _reset_latch():
    doc_identity._reset_absent_log()
    yield
    doc_identity._reset_absent_log()


# ── b) STATUS -> TIER MAP: full enumerated vocabulary ─────────────────────────

def test_status_tier_map_covers_full_vocabulary():
    assert step_10_report._STATUS_TIER_MAP == {
        "current": "confirmed_local",
        "stale": "cache_drift",
        "legacy_unverified": "cache_unverified",
        "current_no_pdf_verify": "cache_unverified",
    }


def test_tier_for_status_none_is_confirmed_local():
    """`_tier_for_status` itself is UNTOUCHED by TODO-380 Phase 2 (its
    None -> confirmed_local mapping stays the correct default for a direct
    caller that supplies `cache_currency=None` to `_apply_tier`). TODO-380
    Phase 2 intercepts BEFORE this function is reached, at the 3 loop call
    sites in build_report (see `_apply_tier_or_never_cached`/
    `_never_cached`) — so the bare `extraction_metadata` shape every fixture
    in test_doc_identity_qualifier.py uses no longer reaches this function
    via those 3 buckets; it's now root-cause-1-shaped and routes to
    not_cache_derived instead (see test_missing_cache_currency_and_
    resolved_key_routes_not_cache_derived below)."""
    assert step_10_report._tier_for_status(None) == "confirmed_local"


# ── _apply_tier: one part backs the finding ───────────────────────────────────

def test_confirmed_local_leaves_label_byte_identical():
    assert step_10_report._apply_tier(LABEL, "current") == (LABEL, "confirmed_local")


@pytest.mark.parametrize("status", ["legacy_unverified", "current_no_pdf_verify"])
def test_cache_unverified_rewrites_prefix_and_appends_tier2_parenthetical(status):
    out, tier = step_10_report._apply_tier(LABEL, status)
    assert out == "Cache-sourced — §5.2 Electrical Characteristics (not locally verified)"
    assert tier == "cache_unverified"


def test_cache_drift_rewrites_prefix_and_appends_tier3_parenthetical():
    out, tier = step_10_report._apply_tier(LABEL, "stale")
    assert out == ("Cache-sourced — §5.2 Electrical Characteristics "
                    "(local datasheet differs from cache source)")
    assert tier == "cache_drift"


def test_non_confirmed_label_still_gets_the_parenthetical_no_prefix_swap():
    """step_08b/step_08c's real production evidence text never starts with
    "Confirmed" (recon report step 1) — the parenthetical must still land."""
    out, tier = step_10_report._apply_tier(FREEFORM_LABEL, "stale")
    assert out == FREEFORM_LABEL + " (local datasheet differs from cache source)"
    assert tier == "cache_drift"


def test_assumed_label_wins_regardless_of_cache_currency():
    """Recon surprise 5: no test anywhere pinned the literal string "Assumed"
    before this. An Assumed label ties to no cache read semantically — it
    tiers as "assumed" and is NEVER rewritten, even backed by a stale cache."""
    out, tier = step_10_report._apply_tier(ASSUMED_LABEL, "stale")
    assert out == ASSUMED_LABEL
    assert tier == "assumed"


def test_assumed_wins_even_with_no_cache_currency_at_all():
    out, tier = step_10_report._apply_tier(ASSUMED_LABEL, None)
    assert out == ASSUMED_LABEL
    assert tier == "assumed"


# ── _apply_tier_many: multi-cache findings (M2 output-conflict) ──────────────

def test_apply_tier_many_cache_drift_outranks_cache_unverified():
    out, tier = step_10_report._apply_tier_many(LABEL, ["legacy_unverified", "stale"])
    assert tier == "cache_drift"
    assert out.endswith("(local datasheet differs from cache source)")


def test_apply_tier_many_all_confirmed_is_unchanged():
    assert step_10_report._apply_tier_many(LABEL, ["current", "current"]) == (
        LABEL, "confirmed_local")


def test_apply_tier_many_no_caches_defaults_confirmed_local():
    assert step_10_report._apply_tier_many(LABEL, []) == (LABEL, "confirmed_local")


# ── composition: tier transform FIRST, doc_identity qualify SECOND ───────────

def _meta(part, cache_currency=None, di=None):
    m = {"pdf_path": f"/x/{part}.pdf", "source_hash": "abc"}
    if cache_currency is not None:
        m["cache_currency"] = cache_currency
    if di is not None:
        m["doc_identity"] = di
    return {part: m}


def _supply_result(part, status="FAIL", label=LABEL):
    return SupplyCheckResult(
        refdes="U1", part_number=part, supply_pin_name="VCC", supply_pin_id="1",
        connected_net="+5V", actual_voltage=5.0, rated_min=3.0, rated_max=3.6,
        rated_abs_max=4.6, status=status, confidence="high", evidence_label=label,
        explanation=None)


def test_tier_rewrite_then_doc_identity_suffix_compose_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", cache_currency="stale", di=MISS),
        output_path=str(out_path), supply_results=[_supply_result("PART-X")])
    entry = report["power_supply_results"][0]
    assert entry["evidence_label"] == (
        "Cache-sourced — §5.2 Electrical Characteristics "
        "(local datasheet differs from cache source); source-text MPN match: none")
    assert entry["evidence_tier"] == "cache_drift"


def test_confirmed_local_does_not_block_doc_identity_qualifier(tmp_path, monkeypatch):
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", cache_currency="current", di=MISS),
        output_path=str(out_path), supply_results=[_supply_result("PART-X")])
    entry = report["power_supply_results"][0]
    assert entry["evidence_label"] == LABEL + "; source-text MPN match: none"
    assert entry["evidence_tier"] == "confirmed_local"


# ── missing cache_currency (pre-368 metadata / test fixtures) ────────────────
#
# TODO-380 Phase 2 narrows this population: `_meta("PART-X")`'s bare shape
# (no `cache_currency`, and — critically — no `resolved` key either) is
# EXACTLY the root-cause-1 shape (`_never_cached` treats a missing
# `resolved` key as falsy) and now correctly routes to `not_cache_derived`
# instead of the old confirmed_local fail-open. `test_doc_identity_
# qualifier.py`'s 19 fixtures use this same bare shape for a DIFFERENT
# reason (they only care about the doc-identity suffix, not the tier), so
# they are unaffected by this narrowing — the doc-identity qualifier
# composes with whichever tier/label the never-cached branch produces, same
# as it always has (see test_tier_rewrite_then_doc_identity_suffix_compose_
# in_order above). The remaining `_tier_for_status(None) == "confirmed_local"`
# contract (untouched, see test_tier_for_status_none_is_confirmed_local)
# still covers a genuinely different, narrower case: a caller that supplies
# `cache_currency=None` explicitly to `_apply_tier` directly, bypassing the
# 3 loop sites' never-cached branch entirely.

def test_missing_cache_currency_and_resolved_key_routes_not_cache_derived(
        tmp_path, monkeypatch):
    """extraction_metadata carrying NO `resolved` key and NO `cache_currency`
    key (the bare pre-368 fixture shape) is root-cause-1-shaped — TODO-380
    Phase 2 routes it to not_cache_derived with the structural/supply
    wording, not confirmed_local."""
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X"),  # no cache_currency, no resolved, no doc_identity
        output_path=str(out_path), supply_results=[_supply_result("PART-X")])
    entry = report["power_supply_results"][0]
    assert entry["evidence_tier"] == "not_cache_derived"
    assert entry["evidence_label"] == (
        LABEL + " (netlist evidence only — no part data in the cache)")


def test_resolved_true_with_no_cache_currency_stays_confirmed_local(
        tmp_path, monkeypatch):
    """A fresh this-run extraction (`resolved: True`, no `cache_currency`
    attached yet — nothing to report a currency status on) is NOT the
    never-cached population: a cache WAS just produced. Must fall through
    to `_apply_tier` and stay confirmed_local, byte-identical, exactly as
    before TODO-380 Phase 2."""
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    out_path = tmp_path / "report.json"
    meta = _meta("PART-X")
    meta["PART-X"]["resolved"] = True
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=meta,
        output_path=str(out_path), supply_results=[_supply_result("PART-X")])
    entry = report["power_supply_results"][0]
    assert entry["evidence_label"] == LABEL
    assert entry["evidence_tier"] == "confirmed_local"


# ── TODO-380 Phase 2: never-cached routing tripwires (invariant of record) ───
# Root cause 1 (attempted, nothing to serve) and root cause 2 (never queued
# at all) both surfaced as a confirmed_local fail-open before this — see
# investigation/recon_reports/todo380_phase1_tier_consumers.md /
# todo380_phase1b_coverage_wording.md. These pin the closed contract at both
# the unit level (_never_cached / _apply_tier_or_never_cached) and
# end-to-end through build_report, across the 2 buckets not already covered
# above (structural + results; power_supply is covered by the two tests
# just above this section).

def test_never_cached_true_when_extraction_metadata_key_is_absent():
    """Root cause 2: the part was never queued for resolution at all (e.g.
    the has_non_power_pin pre-queue filter, pipeline.py:388-398) — no key
    exists for it in extraction_metadata."""
    assert step_10_report._never_cached("GHOST-PART", {}) is True


def test_never_cached_true_for_root_cause_1_shape():
    """Root cause 1: resolution was attempted (an entry exists) but
    produced nothing to serve — resolved is falsy, no cache_currency."""
    meta = {"PART-X": {"pdf_path": None, "resolved": False,
                        "resolve_error": "No datasheet found for 'PART-X'."}}
    assert step_10_report._never_cached("PART-X", meta) is True


def test_never_cached_false_for_fresh_extraction_shape():
    """resolved truthy, no cache_currency yet — a cache WAS just produced
    this run. Must NOT be caught by the never-cached routing."""
    meta = {"PART-X": {"pdf_path": "/x/PART-X.pdf", "resolved": True}}
    assert step_10_report._never_cached("PART-X", meta) is False


def test_never_cached_false_when_cache_currency_present():
    meta = {"PART-X": {"cache_currency": "current"}}
    assert step_10_report._never_cached("PART-X", meta) is False


def test_apply_tier_or_never_cached_routes_absent_part_to_not_cache_derived():
    out, tier = step_10_report._apply_tier_or_never_cached(
        LABEL, "GHOST-PART", {}, step_10_report._W_NETLIST_EVIDENCE_ONLY)
    assert tier == "not_cache_derived"
    assert out == LABEL + " (netlist evidence only — no part data in the cache)"


def test_apply_tier_or_never_cached_assumed_wins_even_when_never_cached():
    """The pre-existing Assumed short-circuit must still win over the new
    never-cached routing — checked first, never reordered."""
    out, tier = step_10_report._apply_tier_or_never_cached(
        ASSUMED_LABEL, "GHOST-PART", {}, step_10_report._W_NETLIST_EVIDENCE_ONLY)
    assert tier == "assumed"
    assert out == ASSUMED_LABEL


def test_tripwire_structural_finding_citing_absent_part_never_confirmed_local(tmp_path):
    """TODO-380 Phase 2 invariant of record: a structural finding citing a
    part with NO extraction_metadata entry at all must NEVER surface as
    confirmed_local (this is the live shape the LT-38 Phase 4 smoke found:
    J1/USB_B_Micro, a connector with no datasheet ever attempted)."""
    r = StructuralCheckResult(
        refdes="J1", part_number="GHOST-CONN", pin_name="GND", pin_id="1",
        pin_kind="ground", connected_net="GND", expected_kind="ground_net",
        status="PASS", confidence="high", evidence_label=LABEL,
        explanation=None, finding_code=None)
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata={},  # GHOST-CONN never appears -> root cause 2
        output_path=str(out_path), structural_results=[r])
    entry = report["structural_integrity_results"][0]
    assert entry["evidence_tier"] == "not_cache_derived"
    assert entry["evidence_tier"] != "confirmed_local"
    assert entry["evidence_label"] == (
        LABEL + " (netlist evidence only — no part data in the cache)")


def test_tripwire_results_bucket_citing_absent_receiver_part_never_confirmed_local(tmp_path, monkeypatch):
    """Same invariant for the results bucket (net-level driver/receiver
    checks) — the largest fail-open population per the Phase 1 census."""
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    r = _check_result(receiver_refdes="U9")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[r], confirmed_voltages={},
        extraction_metadata={},  # RECV-PART never appears -> root cause 2
        output_path=str(out_path), components=[_Comp("U9", "RECV-PART")])
    entry = report["results"][0]
    assert entry["evidence_tier"] == "not_cache_derived"
    assert entry["evidence_tier"] != "confirmed_local"
    assert entry["evidence_label"] == (
        LABEL + " (no part data in the cache — see LIMITATIONS.md to add parts)")


# ── per-bucket wiring ──────────────────────────────────────────────────────────

def test_structural_bucket_gets_evidence_tier_and_tier_rewrite(tmp_path):
    r = StructuralCheckResult(
        refdes="U2", part_number="PART-Y", pin_name="NRST", pin_id="7",
        pin_kind="input", connected_net="NRST", expected_kind="input",
        status="WARN", confidence="medium", evidence_label=LABEL,
        explanation="e", finding_code="F1")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-Y", cache_currency="stale"),
        output_path=str(out_path), structural_results=[r])
    entry = report["structural_integrity_results"][0]
    assert entry["evidence_tier"] == "cache_drift"
    assert entry["evidence_label"] == (
        "Cache-sourced — §5.2 Electrical Characteristics "
        "(local datasheet differs from cache source)")
    # explanation is a raw passthrough field on this bucket, untouched.
    assert entry["explanation"] == "e"


def _check_result(receiver_refdes="U9", label=LABEL):
    return CheckResult(
        net_name="N1", status="FAIL", driver_refdes="U8", driver_voltage=5.0,
        driver_voltage_source="datasheet", receiver_refdes=receiver_refdes,
        receiver_pin_name="IN", receiver_VIH_max=3.6, receiver_abs_max=4.0,
        receiver_confidence="high", driver_confidence="high",
        combined_confidence="high", evidence_label=label,
        unresolvable_reason=None)


class _Comp:
    def __init__(self, refdes, part_number):
        self.refdes, self.part_number = refdes, part_number


def test_results_bucket_backed_by_receiver_part(tmp_path, monkeypatch):
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    r = _check_result(receiver_refdes="U9")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[r], confirmed_voltages={},
        extraction_metadata=_meta("RECV-PART", cache_currency="legacy_unverified"),
        output_path=str(out_path), components=[_Comp("U9", "RECV-PART")])
    entry = report["results"][0]
    assert entry["evidence_tier"] == "cache_unverified"
    assert entry["evidence_label"] == (
        "Cache-sourced — §5.2 Electrical Characteristics (not locally verified)")


def test_output_conflict_multi_driver_tier_precedence_through_build_report(tmp_path):
    f = OutputConflictFinding(
        net="D0", evidence_label=LABEL,
        drivers=[{"refdes": "U1", "pin_id": "3", "pin_name": "Q", "pintype": "output"},
                 {"refdes": "U2", "pin_id": "9", "pin_name": "Y", "pintype": "output"}])
    meta = {
        **_meta("PART-A", cache_currency="legacy_unverified"),
        **_meta("PART-B", cache_currency="stale"),
    }
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=meta, output_path=str(out_path),
        components=[_Comp("U1", "PART-A"), _Comp("U2", "PART-B")],
        output_conflict_results=[f])
    entry = report["output_conflict_results"][0]
    assert entry["evidence_tier"] == "cache_drift"  # stale outranks legacy_unverified
    assert entry["evidence_label"].endswith("(local datasheet differs from cache source)")


def test_kb_topology_buckets_are_not_cache_derived_with_wording(tmp_path):
    """TODO-380 Phase 2 (W1a/W1b): the 3 KB/topology buckets are no longer
    text-untouched -- each now appends its own fixed wording suffix naming
    WHY no cache applies (peripheral = MCU KB; pullup_value/pullup_presence
    = netlist topology), mirroring the wording the never-cached branch adds
    for structural/power_supply/results."""
    peripheral = PeripheralFinding(
        violation=PeripheralViolation.NO_PULLUP_DETECTED, severity=Severity.WARN,
        net="SDA", pins=["U1.SDA"], evidence="Confirmed — no pull-up found on SDA")
    pullup_value = PullupValueFinding(
        net="SDA", refdes="R1", value_str="4.7k", ohms=4700.0,
        severity=PullupBand.WARN, band="high", evidence="Assumed — value in WARN band",
        pins=["R1", "U1"])
    pullup_presence = PullupPresenceFinding(
        net="MISO", family="spi_flash_cs", violation="NO_PULLUP_DETECTED",
        pins=["U2.CS"], evidence="Confirmed — no pull-up on CS")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-Z", cache_currency="stale"),
        output_path=str(out_path),
        peripheral_results=[peripheral], pullup_results=[pullup_value],
        pullup_presence_results=[pullup_presence])
    p = report["peripheral_integrity_results"][0]
    v = report["pullup_value_results"][0]
    pr = report["pullup_presence_results"][0]
    assert p["evidence_tier"] == "not_cache_derived"
    assert p["evidence"] == (
        "Confirmed — no pull-up found on SDA (MCU KB — no datasheet required)")
    assert v["evidence_tier"] == "not_cache_derived"
    assert v["evidence"] == (
        "Assumed — value in WARN band (netlist topology — no datasheet required)")
    assert pr["evidence_tier"] == "not_cache_derived"
    assert pr["evidence"] == (
        "Confirmed — no pull-up on CS (netlist topology — no datasheet required)")


# ── summary + header surface ──────────────────────────────────────────────────

def test_summary_evidence_tier_checks_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    r = _check_result(receiver_refdes="U9")
    supply = _supply_result("PART-CONFIRMED")
    peripheral = PeripheralFinding(
        violation=PeripheralViolation.NO_PULLUP_DETECTED, severity=Severity.WARN,
        net="SDA", pins=["U1.SDA"], evidence="e")
    meta = {
        **_meta("RECV-PART", cache_currency="stale"),
        **_meta("PART-CONFIRMED", cache_currency="current"),
    }
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[r], confirmed_voltages={},
        extraction_metadata=meta, output_path=str(out_path),
        components=[_Comp("U9", "RECV-PART")],
        supply_results=[supply], peripheral_results=[peripheral])
    counts = report["summary"]["evidence_tier_checks"]
    assert counts["cache_drift_count"] == 1       # the results-bucket finding
    assert counts["confirmed_local_count"] == 1    # the supply finding
    assert counts["not_cache_derived_count"] == 1  # the peripheral finding
    total = sum(counts.values())
    assert total == 3


def test_cache_version_is_null_absent_manifest(tmp_path, monkeypatch):
    """Unchanged contract: no manifest -> null cache_version.

    Now establishes its own premise instead of relying on there being no
    manifest anywhere on disk. That global absence was true when TODO-368 wrote
    this (D368-E: _cache_manifest_version was an unconditional None), and
    LT-38 Phase 3b deliberately ends it by shipping
    datasheets_parsed/CACHE_MANIFEST.json — so the test has to point at a
    manifest-free tree to test what its name says."""
    from steps import step_03_resolver as r3
    monkeypatch.setattr(r3, "PARSED_DIR", str(tmp_path / "no_manifest_here"))
    monkeypatch.setitem(r3._manifest_state, "dir", None)
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata={}, output_path=str(out_path))
    assert report["provenance"]["cache_version"] is None


def test_cache_version_is_populated_when_a_manifest_exists(tmp_path, monkeypatch):
    """The other half of the LT-38 Phase 3b wiring: a present manifest's version
    reaches the report header."""
    from steps import step_03_resolver as r3
    parsed = tmp_path / "parsed"
    parsed.mkdir()
    (parsed / "CACHE_MANIFEST.json").write_text(
        json.dumps({"cache_version": "2026-08-15+deadbeefcafe", "files": {}}))
    monkeypatch.setattr(r3, "PARSED_DIR", str(parsed))
    monkeypatch.setitem(r3._manifest_state, "dir", None)
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata={}, output_path=str(out_path))
    assert report["provenance"]["cache_version"] == "2026-08-15+deadbeefcafe"


def test_drift_warning_present_when_cache_drift_finding_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", cache_currency="stale"),
        output_path=str(out_path), supply_results=[_supply_result("PART-X")])
    assert report["drift_warning"] == step_10_report.DRIFT_WARNING_SENTENCE
    assert json.loads(out_path.read_text())["drift_warning"] == \
        step_10_report.DRIFT_WARNING_SENTENCE


def test_drift_warning_absent_when_no_cache_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", cache_currency="current"),
        output_path=str(out_path), supply_results=[_supply_result("PART-X")])
    assert "drift_warning" not in report


# ── the load-bearing invariant, extended to tier strings (e.3) ───────────────

def test_tier_strings_never_reach_an_llm_prompt(tmp_path, monkeypatch):
    """Every prompt handed to the LLM must carry the RAW, untiered label —
    extends test_doc_identity_qualifier.py's
    test_qualifier_never_reaches_an_llm_prompt to the new tier vocabulary."""
    seen: list[str] = []

    def _capture(prompt, *a, **k):
        seen.append(prompt)
        return "model prose"

    monkeypatch.setattr(step_10_report.ollama_client, "generate", _capture)
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", cache_currency="stale"),
        output_path=str(out_path), supply_results=[_supply_result("PART-X")])

    assert seen, "expected the supply explainer to be invoked"
    for prompt in seen:
        assert step_10_report._TIER2_PARENTHETICAL not in prompt
        assert step_10_report._TIER3_PARENTHETICAL not in prompt
        assert step_10_report._CACHE_SOURCED_PREFIX not in prompt
        assert LABEL in prompt          # the raw label DID reach it
    # ...and the emitted field still carries the tier rewrite.
    assert report["power_supply_results"][0]["evidence_label"].startswith(
        step_10_report._CACHE_SOURCED_PREFIX)


def test_no_prompt_template_can_carry_tier_text():
    """Static guard, mirrors test_no_prompt_template_can_carry_a_qualifier: no
    module-level prompt constant interpolates a tier-rewritten field."""
    for name in dir(step_10_report):
        if not name.endswith("_PROMPT"):
            continue
        template = getattr(step_10_report, name)
        assert step_10_report._TIER2_PARENTHETICAL not in template
        assert step_10_report._TIER3_PARENTHETICAL not in template
        assert step_10_report._CACHE_SOURCED_PREFIX not in template


def test_canned_supply_explanation_carries_tier_rewrite(tmp_path, monkeypatch):
    """TODO-520 E4 reverses TODO-368 P2's confinement for the EMITTED explanation:
    the canned (UNRESOLVABLE) text is the tier-rewritten emitted label. The
    prompt still never sees the rewrite."""
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "unused")
    out_path = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", cache_currency="stale"),
        output_path=str(out_path),
        supply_results=[_supply_result("PART-X", status="UNRESOLVABLE")])
    entry = report["power_supply_results"][0]
    assert entry["explanation"] == entry["evidence_label"] == (
        "Cache-sourced — §5.2 Electrical Characteristics "
        "(local datasheet differs from cache source)")
    assert entry["explanation_source"] == "canned"
