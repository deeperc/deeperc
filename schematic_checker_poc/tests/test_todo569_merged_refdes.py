"""TODO-569: merged refdes — detected in the parser, one record kept, the findings
resting on it relabelled (signal/supply/peripheral/pull-up) or dropped
(structural/output conflict), and disclosed in not_checked_parts.

Synthetic netlist text (tmp_path) and synthetic result objects only.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import provenance  # noqa: E402
from pipeline import classify_report  # noqa: E402
from steps import checker_registry, merged_refdes  # noqa: E402
from steps.m2_output_conflict import OutputConflictFinding  # noqa: E402
from steps.step_02_parser import (  # noqa: E402
    ComponentIR, MergedRefdes, NetIR, NetlistIR, PinIR, parse_netlist,
)
from steps.step_08_checker import CheckResult, SignalUnresolvableReason  # noqa: E402
from steps.step_08b_supply_checker import (  # noqa: E402
    SupplyCheckResult, SupplyUnresolvableReason,
)
from steps.step_08c_structural_checker import StructuralCheckResult  # noqa: E402
from steps.step_08d_peripheral_checker import (  # noqa: E402
    FindingReason, PeripheralFinding, PeripheralViolation, Severity,
)
from steps.step_08e_pullup_value_checker import PullupBand, PullupValueFinding  # noqa: E402
from steps.step_08g_pullup_presence import PullupPresenceFinding  # noqa: E402
from steps.step_10_report import build_report  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "test_netlist" / "stm32_5v_violation.net"
LABEL = "Unresolvable — merged reference designator: "


def _comp(ref, value="V", sheet=None, lib="Device", pins=()):
    sp = f'(sheetpath (names "{sheet}") (tstamps "/x/"))' if sheet is not None else ""
    pn = ""
    if pins:
        pn = "(pins " + " ".join(f'(pin (num "{p}") (name "P{p}"))' for p in pins) + ")"
    return (f'(comp (ref "{ref}") (value "{value}") (footprint "F:P") '
            f'(libsource (lib "{lib}") (part "{value}")) {sp} {pn})')


def _net(name, *nodes):
    return (f'(net (name "{name}") '
            + " ".join(f'(node (ref "{r}") (pin "{p}") (pinfunction "{f}"))'
                       for r, p, f in nodes) + ")")


def _write(tmp_path, comps, nets, name="t.net"):
    text = ('(export (version "E") (components ' + " ".join(comps) + ") (nets "
            + " ".join(nets) + "))")
    p = tmp_path / name
    p.write_text(text)
    return parse_netlist(p)


# ── (1)-(7) parser ────────────────────────────────────────────────────────────

def test_two_records_kept_first(tmp_path):
    ir = _write(tmp_path, [_comp("U1", "A", "/s1/"), _comp("R1"), _comp("U1", "A", "/s2/")],
                [_net("N1", ("U1", "1", "X"), ("R1", "1", "1"))])
    assert [c.refdes for c in ir.components] == ["U1", "R1"]
    m = ir.merged_refdes["U1"]
    assert m.records == 2 and m.sheet_paths == ["/s1/", "/s2/"] and m.test == "records"
    assert m.part_numbers == ["A"] and m.collided_pins == {}
    assert list(ir.merged_refdes) == ["U1"]


def test_pin_on_two_nets(tmp_path):
    ir = _write(tmp_path, [_comp("U1")],
                [_net("NA", ("U1", "14", "X")), _net("NB", ("U1", "14", "X"))])
    m = ir.merged_refdes["U1"]
    assert m.test == "pins" and m.records == 1
    assert m.collided_pins == {"14": ["NA", "NB"]}
    assert m.sheet_paths == [""]
    assert {p.pin_id: p.net for p in ir.components[0].pins}["14"] == "NB"


def test_both(tmp_path):
    ir = _write(tmp_path, [_comp("U1", sheet="/a/"), _comp("U1", sheet="/b/")],
                [_net("NA", ("U1", "1", "X")), _net("NB", ("U1", "1", "X"))])
    assert ir.merged_refdes["U1"].test == "both"
    assert len(ir.components) == 1


def test_node_repeated_in_one_net_not_merged(tmp_path):
    ir = _write(tmp_path, [_comp("U1")],
                [_net("NA", ("U1", "1", "X"), ("U1", "1", "X"))])
    assert ir.merged_refdes == {}


def test_lone_question_ref_not_merged(tmp_path):
    ir = _write(tmp_path, [_comp("U?")], [_net("NA", ("U?", "1", "X"))])
    assert ir.merged_refdes == {}


def test_ref_only_in_net_nodes(tmp_path):
    ir = _write(tmp_path, [_comp("U1")],
                [_net("NA", ("U1", "1", "X"), ("Q7", "2", "B")), _net("NB", ("Q7", "2", "B"))])
    m = ir.merged_refdes["Q7"]
    assert m.records == 0 and m.test == "pins" and m.sheet_paths == [] and m.part_numbers == []
    assert m.collided_pins == {"2": ["NA", "NB"]}


def test_no_merged_ref_fixture():
    ir = parse_netlist(FIXTURE)
    assert ir.merged_refdes == {}
    assert [c.refdes for c in ir.components] == ["U1", "U2", "J1"]


# ── synthetic IR helpers ──────────────────────────────────────────────────────

def _ir(merged=(), comps=(), nets=()):
    m = {r: MergedRefdes(records=1, sheet_paths=[""], part_numbers=["X"],
                         collided_pins={"1": ["A", "B"]}, test="pins") for r in merged}
    return NetlistIR(source_file="t", components=list(comps), nets=list(nets),
                     merged_refdes=m)


def _sig(net, status, drv, rcv, reason=None):
    return CheckResult(net_name=net, status=status, driver_refdes=drv, driver_voltage=3.3,
                       driver_voltage_source="rail", receiver_refdes=rcv,
                       receiver_pin_name="IN", receiver_VIH_max=None, receiver_abs_max=3.6,
                       receiver_confidence="high", driver_confidence="high",
                       combined_confidence="high", evidence_label="orig",
                       unresolvable_reason=reason, driver_pin_name="OUT",
                       driver_selection="sel")


def _sup(ref, status, reason=None):
    return SupplyCheckResult(refdes=ref, part_number="P", supply_pin_name="VCC",
                             supply_pin_id="1", connected_net="VCC_3V3", actual_voltage=3.3,
                             rated_min=3.0, rated_max=3.6, rated_abs_max=4.0, status=status,
                             confidence="high", evidence_label="orig", explanation="ex",
                             unresolvable_reason=reason)


def _st(ref, status):
    return StructuralCheckResult(refdes=ref, part_number="P", pin_id="1", pin_name="VCC",
                                 pin_kind="power", connected_net="N", expected_kind="power_rail",
                                 status=status, confidence="high", evidence_label="e")


# ── (8)-(11) relabel / drop ───────────────────────────────────────────────────

def test_relabel_signal():
    a = _sig("N1", "FAIL", "U1", "U2")
    b = _sig("N2", "PASS", "U3", "U1")
    c = _sig("N3", "PASS", "U3", "U4")
    before_a = dict(vars(a))
    c_before = dict(vars(c))
    rows = [a, b, c]
    out = merged_refdes.relabel_signal(rows, _ir(["U1"]))
    assert out is rows and out[2] is c and vars(c) == c_before
    for r in (a, b):
        assert r.status == "UNRESOLVABLE"
        assert r.unresolvable_reason is SignalUnresolvableReason.MERGED_REFDES
        assert r.evidence_label == LABEL + "U1"
        assert r.unresolvable_detail == (
            "Not evaluated: U1 is a merged reference designator in this netlist (several "
            "parts share it, or one of its pins is on more than one net), so its "
            "pin-to-net data cannot be trusted. See not_checked_parts.")
    changed = {"status", "unresolvable_reason", "evidence_label", "unresolvable_detail"}
    assert {k: v for k, v in vars(a).items() if k not in changed} == \
        {k: v for k, v in before_a.items() if k not in changed}


def test_relabel_signal_two_refs_sorted():
    a = _sig("N1", "PASS", "U9", "U1")
    merged_refdes.relabel_signal([a], _ir(["U1", "U9"]))
    assert a.evidence_label == LABEL + "U1, U9"


def test_relabel_supply():
    p = _sup("U1", "PASS")
    u = _sup("U1", "UNRESOLVABLE", SupplyUnresolvableReason.SPEC_NOT_EXTRACTED)
    k = _sup("U2", "PASS")
    merged_refdes.relabel_supply([p, u, k], _ir(["U1"]))
    for r in (p, u):
        assert r.status == "UNRESOLVABLE"
        assert r.unresolvable_reason is SupplyUnresolvableReason.MERGED_REFDES
        assert r.evidence_label == LABEL + "U1"
        assert r.explanation == "ex"
    assert k.status == "PASS" and k.unresolvable_reason is None


def test_drop_structural_and_output_conflict():
    rows = [_st("U2", "PASS"), _st("U1", "FAIL"), _st("U3", "WARN")]
    out = merged_refdes.drop_structural(rows, _ir(["U1"]))
    assert [r.refdes for r in out] == ["U2", "U3"]
    oc = [OutputConflictFinding(net="N", drivers=[{"refdes": "U1"}, {"refdes": "U2"}],
                                evidence_label="e"),
          OutputConflictFinding(net="M", drivers=[{"refdes": "U3"}, {"refdes": "U2"}],
                                evidence_label="e")]
    out = merged_refdes.drop_output_conflict(oc, _ir(["U1"]))
    assert [r.net for r in out] == ["M"]


def test_empty_merged_returns_same_list():
    rows = [_st("U1", "FAIL")]
    ir = _ir()
    for fn in (merged_refdes.drop_structural, merged_refdes.relabel_supply):
        assert fn(rows, ir) is rows
    assert len(rows) == 1
    assert merged_refdes.relabel_signal(rows, SimpleNamespace()) is rows


def test_token_refs_and_relabel_via_tokens():
    refs = {"R1", "R10", "U?"}
    assert merged_refdes.token_refs(["R10.2"], refs) == {"R10"}
    assert merged_refdes.token_refs(["R1.2"], refs) == {"R1"}
    assert merged_refdes.token_refs(["U?.8"], refs) == {"U?"}
    assert merged_refdes.token_refs(["X9.1"], refs) == {"X9"}
    comps = [ComponentIR(refdes=r, part_number="", value="", pins=[]) for r in sorted(refs)]
    ir = _ir(["U?"], comps=comps)
    pf = PeripheralFinding(violation=PeripheralViolation.ROLE_MISMATCH, severity=Severity.FAIL,
                           net="SDA", pins=["U?.8", "R10.2"], evidence="e")
    keep = PeripheralFinding(violation=PeripheralViolation.ROLE_MISMATCH,
                             severity=Severity.WARN, net="SCL", pins=["R1.2"], evidence="e")
    merged_refdes.relabel_peripheral([pf, keep], ir)
    assert pf.severity is Severity.UNRESOLVABLE and pf.reason is FindingReason.MERGED_REFDES
    assert pf.evidence == LABEL + "U?; this check is not evaluated on this net"
    assert keep.severity is Severity.WARN and keep.reason is None
    pp = PullupPresenceFinding(net="SDA", family="f", violation="v", pins=["U?.8"],
                               evidence="e")
    merged_refdes.relabel_pullup_presence([pp], ir)
    assert pp.severity == "UNRESOLVABLE" and pp.reason == "MERGED_REFDES"
    by_ref = PullupValueFinding(net="SDA", refdes="U?", value_str="1M", ohms=1e6,
                                severity=PullupBand.WARN, band="b", evidence="e")
    by_pin = PullupValueFinding(net="SCL", refdes="R1", value_str="1M", ohms=1e6,
                                severity=PullupBand.WARN, band="b", evidence="e",
                                pins=["R1", "U?"])
    other = PullupValueFinding(net="X", refdes="R10", value_str="1M", ohms=1e6,
                               severity=PullupBand.WARN, band="b", evidence="e", pins=["R10"])
    merged_refdes.relabel_pullup_value([by_ref, by_pin, other], ir)
    for r in (by_ref, by_pin):
        assert r.severity is PullupBand.UNRESOLVABLE and r.reason == "MERGED_REFDES"
    assert other.severity is PullupBand.WARN and other.reason is None


# ── (12) step_10 + classify_report ────────────────────────────────────────────

def test_build_report_reason_and_counts(tmp_path):
    ir = _ir(["U1"])
    pv = [PullupValueFinding(net="SDA", refdes="U1", value_str="1M", ohms=1e6,
                             severity=PullupBand.WARN, band="b", evidence="e"),
          PullupValueFinding(net="SCL", refdes="R2", value_str="1M", ohms=1e6,
                             severity=PullupBand.WARN, band="b", evidence="e")]
    merged_refdes.relabel_pullup_value(pv, ir)
    sig = merged_refdes.relabel_signal([_sig("N1", "PASS", "U1", "U2")], ir)
    sup = merged_refdes.relabel_supply([_sup("U1", "PASS")], ir)
    per = [PeripheralFinding(violation=PeripheralViolation.ROLE_MISMATCH,
                             severity=Severity.FAIL, net="SDA", pins=["U1.3"], evidence="e")]
    merged_refdes.relabel_peripheral(per, ir)
    pp = [PullupPresenceFinding(net="SDA", family="f", violation="v", pins=["U1.3"],
                                evidence="e")]
    merged_refdes.relabel_pullup_presence(pp, ir)
    rep = build_report(source_netlist="", results=sig, confirmed_voltages={},
                       extraction_metadata={}, output_path=str(tmp_path / "r.json"),
                       supply_results=sup, peripheral_results=per, pullup_results=pv,
                       pullup_presence_results=pp)
    assert [r["reason"] for r in rep["pullup_value_results"]] == ["MERGED_REFDES", None]
    s = rep["summary"]
    assert s["unresolvable"] == 1 and s["supply_checks"]["unresolvable"] == 1
    assert s["pullup_value_checks"]["unresolvable"] == 1
    assert s["peripheral_checks"]["unresolvable"] == 1
    assert s["pullup_presence_checks"]["unresolvable"] == 1
    assert rep["results"][0]["unresolvable_reason"] == "MERGED_REFDES"
    assert rep["results"][0]["explanation"].startswith("Not evaluated: U1")
    assert rep["power_supply_results"][0]["unresolvable_reason"] == "MERGED_REFDES"
    rep["pullup_value_results"] = rep["pullup_value_results"][:1]
    assert classify_report(rep) == "has_unresolvable_only"


# ── (13) disclosures ──────────────────────────────────────────────────────────

def test_disclosures_and_one_non_ic_entry(tmp_path):
    ir = _write(tmp_path,
                [_comp("U1", "MCU"),
                 _comp("J1", "Conn", "/a/", lib="Connector_Generic"),
                 _comp("J1", "Conn", "/b/", lib="Connector_Generic")],
                [_net("VCC_3V3", ("U1", "2", "VCC")),
                 _net("SIG", ("U1", "1", "PA0"), ("J1", "1", "1"))])
    ir.power_nets = ["VCC_3V3"]
    d = merged_refdes.disclosures(ir)
    assert d == [{"refdes": "J1", "part_number": "Conn", "checker": "all",
                  "reason": "MERGED_REFDES", "records": 2, "sheet_paths": ["/a/", "/b/"],
                  "part_numbers": ["Conn"], "collided_pins": [], "test": "records"}]
    assert list(d[0]) == ["refdes", "part_number", "checker", "reason", "records",
                          "sheet_paths", "part_numbers", "collided_pins", "test"]
    ctx = SimpleNamespace(ir=ir, pin_specs={}, confirmed_voltages={"VCC_3V3": 3.3},
                          rail_provenance=None, not_checked_parts=[])
    checker_registry.BY_STEP["08"].run(ctx)
    reasons = [(e["refdes"], e["reason"]) for e in ctx.not_checked_parts]
    assert reasons == [("J1", "NON_IC_PART"), ("J1", "MERGED_REFDES")]


def test_disclosure_sort_and_collided_and_records_zero():
    ir = NetlistIR(source_file="t", components=[], nets=[], merged_refdes={
        "U2": MergedRefdes(records=0, sheet_paths=[], part_numbers=[],
                           collided_pins={"9": ["A", "B"], "10": ["C", "D"]}, test="pins"),
        "U1": MergedRefdes(records=0, sheet_paths=[], part_numbers=[],
                           collided_pins={"1": ["A", "B"]}, test="pins")})
    d = merged_refdes.disclosures(ir)
    assert [e["refdes"] for e in d] == ["U1", "U2"]
    assert d[0]["part_number"] is None
    assert d[1]["collided_pins"] == [{"pin": "10", "nets": ["C", "D"]},
                                     {"pin": "9", "nets": ["A", "B"]}]


# ── (14) registry adapter ─────────────────────────────────────────────────────

def test_registry_supply_adapter(monkeypatch):
    rows = [_sup("U1", "PASS"), _sup("U2", "PASS")]
    monkeypatch.setattr(checker_registry.step_08b_supply_checker,
                        "check_component_supplies", lambda **kw: rows)
    ctx = SimpleNamespace(ir=_ir(["U1"]), seen_parts={}, confirmed_voltages={},
                          derived_provenance={})
    out = checker_registry.BY_STEP["08b"].run(ctx)
    assert [r.status for r in out] == ["UNRESOLVABLE", "PASS"]
    rows2 = [_sup("U1", "PASS")]
    monkeypatch.setattr(checker_registry.step_08b_supply_checker,
                        "check_component_supplies", lambda **kw: rows2)
    ctx2 = SimpleNamespace(ir=SimpleNamespace(components=[]), seen_parts={},
                           confirmed_voltages={}, derived_provenance={})
    out2 = checker_registry.BY_STEP["08b"].run(ctx2)
    assert out2 is rows2 and out2[0].status == "PASS"


# ── (15) provenance ───────────────────────────────────────────────────────────

def test_provenance_members():
    assert "steps/step_02_parser.py" in provenance.CHECKER_SOURCE_FILES
    assert "steps/merged_refdes.py" in provenance.CHECKER_SOURCE_FILES


# ── (16) run_checks rendering ─────────────────────────────────────────────────

def test_render_findings():
    import run_checks
    rep = {
        "results": [{"net": "N1", "status": "UNRESOLVABLE",
                     "driver": {"refdes": "U1"}, "receiver": {"refdes": "U2", "pin_name": "IN"},
                     "evidence_label": LABEL + "U1",
                     "unresolvable_reason": "MERGED_REFDES",
                     "explanation": "Not evaluated: U1 is a merged reference designator"}],
        "peripheral_integrity_results": [{
            "net": "SDA", "violation": "ROLE_MISMATCH", "severity": "UNRESOLVABLE",
            "pins": ["U1.3"], "evidence": LABEL + "U1; this check is not evaluated on this net",
            "kb_provenance": [], "reason": "MERGED_REFDES"}],
    }
    text = run_checks._render_findings(rep)
    assert "merged reference designator" in text
