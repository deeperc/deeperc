"""TODO-337 Phase 2b-iii — evidence-label doc-identity qualifiers.

Verdict-neutral by construction: these tests pin the qualifier TEXT and the
places it may and may not appear. The load-bearing one is
`test_qualifier_never_reaches_an_llm_prompt` — the qualifier is annotation for a
human reader, and letting it into a Gemma prompt would feed model-authored prose
a signal the model was never asked to reason about.
"""
import json
import logging

import pytest

from steps import doc_identity
from steps import step_10_report
from steps.step_08b_supply_checker import SupplyCheckResult
from steps.step_08c_structural_checker import StructuralCheckResult
from steps.m2_output_conflict import OutputConflictFinding


STRONG = {"class": "STRONG", "tier": 1, "depth": 0, "ratio": 1.0}
WEAK = {"class": "WEAK", "tier": 3, "depth": 1, "ratio": 0.5}
MISS = {"class": "MISS", "tier": None, "depth": None, "ratio": None}
UNVERIFIABLE = {"class": "unverifiable", "tier": None, "depth": None, "ratio": None}

LABEL = "Confirmed — §5.2 Electrical Characteristics"


@pytest.fixture(autouse=True)
def _reset_latch():
    doc_identity._reset_absent_log()
    yield
    doc_identity._reset_absent_log()


# ── one test per class ───────────────────────────────────────────────────────

def test_miss_appends_the_miss_qualifier():
    assert doc_identity.qualify(LABEL, MISS) == LABEL + "; source-text MPN match: none"
    assert doc_identity.QUALIFIER_MISS == "; source-text MPN match: none"


def test_unverifiable_appends_the_unverifiable_qualifier():
    assert (doc_identity.qualify(LABEL, UNVERIFIABLE)
            == LABEL + "; source document unverifiable")
    assert doc_identity.QUALIFIER_UNVERIFIABLE == "; source document unverifiable"


def test_strong_leaves_the_label_byte_identical():
    assert doc_identity.qualify(LABEL, STRONG) == LABEL


def test_weak_leaves_the_label_byte_identical():
    """WEAK is a real positive — a discriminating head of the stem WAS found."""
    assert doc_identity.qualify(LABEL, WEAK) == LABEL


# ── defensive: absent key ────────────────────────────────────────────────────

def test_absent_doc_identity_leaves_the_label_unchanged():
    assert doc_identity.qualify(LABEL, None) == LABEL
    assert doc_identity.qualify(LABEL, {}) == LABEL


def test_unrecognised_class_leaves_the_label_unchanged():
    assert doc_identity.qualify(LABEL, {"class": "SOMETHING_NEW"}) == LABEL


def test_absent_key_logs_once_at_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger="steps.doc_identity"):
        for _ in range(5):
            doc_identity.qualify(LABEL, None)
    lines = [r for r in caplog.records if "doc-identity" in r.message]
    assert len(lines) == 1, f"expected exactly one debug line, got {len(lines)}"
    assert lines[0].levelno == logging.DEBUG


def test_absent_key_does_not_log_above_debug(caplog):
    with caplog.at_level(logging.INFO, logger="steps.doc_identity"):
        doc_identity.qualify(LABEL, None)
    assert not caplog.records


# ── multi-cache findings (the M2 output-conflict bucket) ─────────────────────

def test_qualify_many_appends_at_most_one_qualifier():
    out = doc_identity.qualify_many(LABEL, [MISS, UNVERIFIABLE, STRONG])
    assert out == LABEL + "; source-text MPN match: none"   # MISS outranks


def test_qualify_many_all_positive_is_unchanged():
    assert doc_identity.qualify_many(LABEL, [STRONG, WEAK]) == LABEL


def test_qualify_many_no_caches_is_unchanged():
    assert doc_identity.qualify_many(LABEL, []) == LABEL
    assert doc_identity.qualify_many(LABEL, [None, None]) == LABEL


# ── wiring: the report's emitted fields ──────────────────────────────────────

def _supply_result(part, status="FAIL"):
    return SupplyCheckResult(
        refdes="U1", part_number=part, supply_pin_name="VCC", supply_pin_id="1",
        connected_net="+5V", actual_voltage=5.0, rated_min=3.0, rated_max=3.6,
        rated_abs_max=4.6, status=status, confidence="high", evidence_label=LABEL,
        explanation=None)


def _meta(part, di):
    return {part: {"pdf_path": f"/x/{part}.pdf", "source_hash": "abc",
                   "doc_identity": di}}


def test_supply_evidence_label_is_qualified(tmp_path, monkeypatch):
    """TODO-380 Phase 2: `_meta()`'s bare shape (no `resolved`, no
    `cache_currency`) is root-cause-1-shaped, so the tier-rewrite wording now
    lands BEFORE the doc-identity suffix — composition order unchanged, this
    file's fixture shape just moved populations (see step_10_report's
    `_never_cached`)."""
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    out = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", MISS), output_path=str(out),
        supply_results=[_supply_result("PART-X")])
    label = report["power_supply_results"][0]["evidence_label"]
    assert label == (
        LABEL + step_10_report._W_NETLIST_EVIDENCE_ONLY
        + "; source-text MPN match: none")
    assert json.loads(out.read_text())["power_supply_results"][0][
        "evidence_label"] == label


def test_structural_evidence_label_is_qualified(tmp_path):
    r = StructuralCheckResult(
        refdes="U2", part_number="PART-Y", pin_name="NRST", pin_id="7",
        pin_kind="input", connected_net="NRST", expected_kind="input",
        status="WARN", confidence="medium", evidence_label=LABEL,
        explanation="e", finding_code="F1")
    out = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-Y", UNVERIFIABLE), output_path=str(out),
        structural_results=[r])
    assert (report["structural_integrity_results"][0]["evidence_label"]
            == LABEL + step_10_report._W_NETLIST_EVIDENCE_ONLY
            + "; source document unverifiable")


def test_output_conflict_label_qualified_from_any_cited_driver(tmp_path):
    class _C:
        def __init__(self, refdes, part_number):
            self.refdes, self.part_number = refdes, part_number

    f = OutputConflictFinding(
        net="D0", evidence_label=LABEL,
        drivers=[{"refdes": "U1", "pin_id": "3", "pin_name": "Q", "pintype": "output"},
                 {"refdes": "U2", "pin_id": "9", "pin_name": "Y", "pintype": "output"}])
    meta = {**_meta("PART-A", STRONG), **_meta("PART-B", MISS)}
    out = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=meta, output_path=str(out),
        components=[_C("U1", "PART-A"), _C("U2", "PART-B")],
        output_conflict_results=[f])
    assert (report["output_conflict_results"][0]["evidence_label"]
            == LABEL + "; source-text MPN match: none")


def test_a_strong_cache_still_gets_only_the_never_cached_wording(tmp_path, monkeypatch):
    """Renamed (TODO-380 Phase 2): this fixture's bare shape is root-cause-1
    -shaped, so it's no longer byte-identical to LABEL — it now carries the
    never-cached wording. STRONG doc-identity still contributes no suffix of
    its own (a real positive appends nothing), so the wording is the ONLY
    change, unaffected by the doc-identity axis."""
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    out = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", STRONG), output_path=str(out),
        supply_results=[_supply_result("PART-X")])
    assert report["power_supply_results"][0]["evidence_label"] == (
        LABEL + step_10_report._W_NETLIST_EVIDENCE_ONLY)


def test_verdict_fields_are_untouched_by_a_miss(tmp_path, monkeypatch):
    """The whole layer is text-only: status/confidence/summary counts unmoved."""
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "explained")
    reports = []
    for di in (STRONG, MISS):
        out = tmp_path / f"report_{di['class']}.json"
        reports.append(step_10_report.build_report(
            source_netlist="n.net", results=[], confirmed_voltages={},
            extraction_metadata=_meta("PART-X", di), output_path=str(out),
            supply_results=[_supply_result("PART-X")]))
    a, b = reports
    assert a["summary"] == b["summary"]
    for key in ("status", "confidence", "refdes", "part_number", "actual_voltage_v"):
        assert (a["power_supply_results"][0][key]
                == b["power_supply_results"][0][key])


# ── the load-bearing invariant ───────────────────────────────────────────────

def test_qualifier_never_reaches_an_llm_prompt(tmp_path, monkeypatch):
    """Every prompt handed to the LLM must carry the RAW label."""
    seen: list[str] = []

    def _capture(prompt, *a, **k):
        seen.append(prompt)
        return "model prose"

    monkeypatch.setattr(step_10_report.ollama_client, "generate", _capture)
    out = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", MISS), output_path=str(out),
        supply_results=[_supply_result("PART-X")])

    assert seen, "expected the supply explainer to be invoked"
    for prompt in seen:
        assert doc_identity.QUALIFIER_MISS not in prompt
        assert doc_identity.QUALIFIER_UNVERIFIABLE not in prompt
        assert LABEL in prompt          # the raw label DID reach it
    # ...and the emitted field still carries the qualifier.
    assert report["power_supply_results"][0]["evidence_label"].endswith(
        doc_identity.QUALIFIER_MISS)


def test_no_prompt_template_can_carry_a_qualifier():
    """Static guard: no module-level prompt constant interpolates a qualified
    field. Catches a future prompt that starts reading the emitted label."""
    for name in dir(step_10_report):
        if not name.endswith("_PROMPT"):
            continue
        template = getattr(step_10_report, name)
        assert doc_identity.QUALIFIER_MISS not in template
        assert doc_identity.QUALIFIER_UNVERIFIABLE not in template


def test_explanation_field_is_never_qualified(tmp_path, monkeypatch):
    """P2's scope: deltas are confined to evidence_label. The UNRESOLVABLE branch
    of _supply_explain passes the label straight through — unqualified."""
    monkeypatch.setattr(step_10_report.ollama_client, "generate",
                        lambda *a, **k: "unused")
    out = tmp_path / "report.json"
    report = step_10_report.build_report(
        source_netlist="n.net", results=[], confirmed_voltages={},
        extraction_metadata=_meta("PART-X", MISS), output_path=str(out),
        supply_results=[_supply_result("PART-X", status="UNRESOLVABLE")])
    entry = report["power_supply_results"][0]
    assert entry["explanation"] == LABEL
    assert entry["evidence_label"] == (
        LABEL + step_10_report._W_NETLIST_EVIDENCE_ONLY + doc_identity.QUALIFIER_MISS)
