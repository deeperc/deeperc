"""TODO-557 — signal-check driver eligibility, NO_DRIVER_ON_NET rows, signal reason
codes (SignalUnresolvableReason) and the driver pin_name / selection report fields
(TODO-558 sections 5-6)."""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import run_checks as rct  # noqa: E402
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR  # noqa: E402
from steps.step_05_validator import ValidatedPinSpec  # noqa: E402
from steps.step_08_checker import (  # noqa: E402
    SignalUnresolvableReason as R,
    _driver_ineligible,
    _unresolved_driver_voltage_reason,
    run,
)
from steps.step_10_report import build_report  # noqa: E402

NO_DRIVER_DETAIL = ("No driver on this net in the netlist: every endpoint is an input "
                    "pin, an open-drain pin or a non-IC part")
NO_DRIVER_LABEL = "Unresolvable — no driver on this net in the netlist"
CONFIRMED = {"VCC_5V": 5.0, "VCC_3V3": 3.3}


# ── fixtures ──────────────────────────────────────────────────────────────────

def _ir(*comps):
    """comps: (refdes, [(pin_id, pin_name, net)], extra ComponentIR kwargs)."""
    components, by_net = [], {}
    for refdes, pins, extra in comps:
        components.append(ComponentIR(
            refdes=refdes, part_number=f"PN_{refdes}", value=f"PN_{refdes}",
            pins=[PinIR(pin_id=i, pin_name=n, net=net) for i, n, net in pins], **extra))
        for i, _n, net in pins:
            by_net.setdefault(net, []).append((refdes, i))
    nets = [NetIR(name=n, pins=p) for n, p in by_net.items()]
    power = [n for n in by_net if n in CONFIRMED]
    return NetlistIR(source_file="test", components=components, nets=nets,
                     power_nets=power, ground_nets=[])


def _ic(refdes, pin_name="IO", rail="VCC_3V3"):
    pins = [("1", pin_name, "SIG")]
    if rail:
        pins.append(("2", "VCC", rail))
    return (refdes, pins, {})


def _gap(refdes, pin_name="1"):
    # A connector by library, refdes the passive regex misses (TODO-554 GAP part).
    return (refdes, [("1", pin_name, "SIG")], {"libsource_lib": "Connector_Generic"})


def _spec(pin_name="IO", pin_type="bidirectional", exact=True, open_drain=False,
          abs_max=None, VIH_min=None, VIH_max=None, notes=None, conf="high"):
    return ValidatedPinSpec(
        part_number="X", pin_name=pin_name, pin_id="1", VIH_max=VIH_max,
        VIH_min=VIH_min, absolute_max_voltage=abs_max, pin_type=pin_type,
        source_snippet="snippet", signal_score=75, confidence=conf, is_verified=True,
        pin_type_exact=exact, open_drain=open_drain, expr_notes=list(notes or []))


def _sig_rows(ir, specs):
    return [r for r in run(ir, specs, CONFIRMED) if r.net_name == "SIG"]


# ── _driver_ineligible ────────────────────────────────────────────────────────

@pytest.mark.parametrize("p, expected", [
    ({"pin_type": "digital_input", "pin_type_exact": True}, True),
    ({"pin_type": "input", "pin_type_exact": True}, True),
    ({"pin_type": "digital_input", "pin_type_exact": False}, False),
    ({"pin_type": "input", "pin_type_exact": False}, False),
    ({"pin_type": "output", "pin_type_exact": True, "open_drain": True}, True),
    ({"pin_type": None, "pin_type_exact": False, "open_drain": True}, True),
    ({"pin_type": "analog", "pin_type_exact": True}, False),
    ({"pin_type": "bidirectional", "pin_type_exact": True}, False),
    ({}, False),
])
def test_driver_ineligible(p, expected):
    assert _driver_ineligible(p) is expected


# ── driver choice ─────────────────────────────────────────────────────────────

def test_exact_input_with_highest_rail_is_not_driver():
    ir = _ir(_ic("U_IN", "IN", "VCC_5V"), _ic("U_B", "IO", "VCC_3V3"))
    specs = {("U_IN", "IN"): _spec("IN", "input", abs_max=6.0),
             ("U_B", "IO"): _spec("IO", "bidirectional", abs_max=6.0)}
    rows = _sig_rows(ir, specs)
    assert len(rows) == 1
    r = rows[0]
    assert r.driver_refdes == "U_B" and r.receiver_refdes == "U_IN"
    assert r.driver_selection == "highest_rail" and r.driver_pin_name == "IO"


def test_explicit_output_still_drives():
    ir = _ir(_ic("U_DRV", "OUT", "VCC_5V"), _ic("U_RCV", "IN", "VCC_3V3"))
    specs = {("U_DRV", "OUT"): _spec("OUT", "output"),
             ("U_RCV", "IN"): _spec("IN", "input", abs_max=3.6)}
    [r] = _sig_rows(ir, specs)
    assert r.driver_refdes == "U_DRV" and r.status == "FAIL"
    assert r.driver_selection == "explicit_output" and r.driver_pin_name == "OUT"
    assert r.unresolvable_reason is None and r.unresolvable_detail is None


def test_first_pin_selection():
    ir = _ir(_ic("U_A", "IO", None), _ic("U_B", "IO", None))
    specs = {("U_B", "IO"): _spec("IO", "bidirectional", abs_max=3.6)}
    rows = _sig_rows(ir, specs)
    assert [r.driver_refdes for r in rows] == ["U_A"]
    assert rows[0].driver_selection == "first_pin" and rows[0].driver_pin_name == "IO"


def test_two_exact_inputs_give_two_no_driver_rows():
    ir = _ir(_ic("U1", "IN", "VCC_5V"), _ic("U2", "IN", "VCC_3V3"))
    specs = {("U1", "IN"): _spec("IN", "digital_input", abs_max=6.0, VIH_min=2.0),
             ("U2", "IN"): _spec("IN", "input")}
    rows = _sig_rows(ir, specs)
    assert sorted(r.receiver_refdes for r in rows) == ["U1", "U2"]
    for r in rows:
        assert r.status == "UNRESOLVABLE"
        assert r.unresolvable_reason is R.NO_DRIVER_ON_NET
        assert r.unresolvable_detail == NO_DRIVER_DETAIL
        assert r.evidence_label == NO_DRIVER_LABEL
        assert r.driver_refdes is None and r.driver_voltage is None
        assert r.driver_pin_name is None and r.driver_selection == "none"
        assert r.driver_voltage_source == "unknown"
        assert r.driver_confidence == "low" and r.combined_confidence == "low"
    u1 = next(r for r in rows if r.receiver_refdes == "U1")
    assert (u1.receiver_abs_max, u1.receiver_VIH_min, u1.receiver_confidence) == (6.0, 2.0, "high")


def test_no_driver_row_receiver_without_spec():
    ir = _ir(_ic("U1", "IN", "VCC_5V"), _ic("U2", "IO", "VCC_3V3"))
    specs = {("U1", "IN"): _spec("IN", "input"),
             ("U2", "IO"): _spec("IO", "output", open_drain=True)}
    rows = _sig_rows(ir, specs)
    assert len(rows) == 2
    assert all(r.unresolvable_reason is R.NO_DRIVER_ON_NET for r in rows)


def test_open_drain_never_driver():
    ir = _ir(_ic("U_OD", "FLG", "VCC_5V"), _ic("U_B", "IO", "VCC_3V3"))
    specs = {("U_OD", "FLG"): _spec("FLG", "output", open_drain=True),
             ("U_B", "IO"): _spec("IO", "bidirectional", abs_max=6.0)}
    rows = _sig_rows(ir, specs)
    assert [r.driver_refdes for r in rows] == ["U_B"]
    assert rows[0].receiver_refdes == "U_OD"


def test_one_exact_input_plus_passives_no_row():
    ir = _ir(_ic("U1", "IN", "VCC_3V3"), ("R1", [("1", "1", "SIG"), ("2", "2", "VCC_3V3")], {}))
    specs = {("U1", "IN"): _spec("IN", "input", abs_max=3.6)}
    assert _sig_rows(ir, specs) == []


def test_only_gap_endpoints_no_row():
    ir = _ir(_gap("U5"), _gap("U6"))
    specs = {("U5", "1"): _spec("1", "input", abs_max=3.6),
             ("U6", "1"): _spec("1", "input", abs_max=3.6)}
    assert _sig_rows(ir, specs) == []


def test_gap_receiver_with_limits_plus_exact_input_two_rows():
    ir = _ir(_gap("U5"), _ic("U1", "IN", "VCC_3V3"))
    specs = {("U5", "1"): _spec("1", "bidirectional", abs_max=3.6),
             ("U1", "IN"): _spec("IN", "input", abs_max=3.6)}
    rows = _sig_rows(ir, specs)
    assert sorted(r.receiver_refdes for r in rows) == ["U1", "U5"]
    assert all(r.unresolvable_reason is R.NO_DRIVER_ON_NET for r in rows)


def test_r7_no_driver_row_keeps_reason_with_expr_notes():
    ir = _ir(_ic("U1", "IN", "VCC_5V"), _ic("U2", "IN", "VCC_3V3"))
    specs = {("U1", "IN"): _spec("IN", "input", notes=["VIH_min unevaluated: X"]),
             ("U2", "IN"): _spec("IN", "input")}
    rows = _sig_rows(ir, specs)
    u1 = next(r for r in rows if r.receiver_refdes == "U1")
    assert u1.unresolvable_reason is R.NO_DRIVER_ON_NET
    assert u1.unresolvable_detail == NO_DRIVER_DETAIL
    assert u1.evidence_label == NO_DRIVER_LABEL + " [VIH_min unevaluated: X]"


# ── reason codes: each site, detail byte-identical to the pre-557 string ───────

def test_code_no_driver_voltage_no_receiver_specs():
    ir = _ir(_ic("U_DRV", "OUT", None), _ic("U_RCV", "IN", None))
    [r] = _sig_rows(ir, {("U_DRV", "OUT"): _spec("OUT", "output")})
    assert r.unresolvable_reason is R.NO_DRIVER_VOLTAGE_NO_RECEIVER_SPECS
    assert r.unresolvable_detail == "Neither driver voltage nor receiver pin specs could be determined"


def test_code_driver_no_confirmed_rail_from_run():
    ir = _ir(_ic("U_DRV", "OUT", None), _ic("U_RCV", "IN", None))
    [r] = _sig_rows(ir, {("U_DRV", "OUT"): _spec("OUT", "output"),
                         ("U_RCV", "IN"): _spec("IN", "input", abs_max=3.6)})
    assert r.unresolvable_reason is R.DRIVER_NO_CONFIRMED_RAIL
    assert r.unresolvable_detail == "Driver voltage unknown — U_DRV has no confirmed rail on any pin"


def test_codes_unresolved_driver_voltage_helper():
    ir = _ir(("U9", [("1", "OUT", "SIG")], {}),
             ("U14", [("1", "OUT", "SIG"), ("2", "VCCA", "VCC_5V"), ("3", "VCCB", "VCC_3V3")], {}))
    assert _unresolved_driver_voltage_reason("U_GHOST", ir, {}) == (
        R.DRIVER_NO_DATASHEET, "Driver voltage unknown — U_GHOST has no resolved datasheet")
    assert _unresolved_driver_voltage_reason(None, ir, {}) == (
        R.DRIVER_NO_DATASHEET, "Driver voltage unknown — unknown has no resolved datasheet")
    assert _unresolved_driver_voltage_reason("U9", ir, {}) == (
        R.DRIVER_NO_CONFIRMED_RAIL, "Driver voltage unknown — U9 has no confirmed rail on any pin")
    assert _unresolved_driver_voltage_reason("U14", ir, CONFIRMED) == (
        R.DRIVER_DOMAIN_AMBIGUOUS,
        "Driver voltage unknown — U14 power domain ambiguous "
        "(2 rail pin(s), 2 distinct voltage(s): [3.3, 5.0])")


def test_code_receiver_specs_not_extracted():
    ir = _ir(_ic("U_DRV", "OUT", "VCC_5V"), _ic("U_RCV", "IN", "VCC_3V3"))
    [r] = _sig_rows(ir, {("U_DRV", "OUT"): _spec("OUT", "output")})
    assert r.unresolvable_reason is R.RECEIVER_SPECS_NOT_EXTRACTED
    assert r.unresolvable_detail == (
        "Receiver specs unknown — U_RCV pin specs not extracted "
        "(signal_score too low or datasheet section not found)")


def test_code_driver_not_push_pull():
    ir = _ir(_ic("U_DRV", "IO", "VCC_5V"), _ic("U_RCV", "IN", "VCC_3V3"))
    [r] = _sig_rows(ir, {("U_DRV", "IO"): _spec("IO", "bidirectional"),
                         ("U_RCV", "IN"): _spec("IN", "bidirectional", abs_max=3.6)})
    assert r.unresolvable_reason is R.DRIVER_NOT_PUSH_PULL
    assert r.unresolvable_detail == (
        "Driver U_DRV is not a push-pull output; over-voltage verdict from a "
        "non-driving pin's VCC is not trustworthy")
    assert r.driver_selection == "highest_rail"


def test_code_receiver_default_group():
    ir = _ir(_ic("U_DRV", "OUT", "VCC_5V"), _ic("U_RCV", "IN", "VCC_3V3"))
    [r] = _sig_rows(ir, {("U_DRV", "OUT"): _spec("OUT", "output"),
                         ("U_RCV", "IN"): _spec("IN", "input", exact=False, abs_max=3.6)})
    assert r.unresolvable_reason is R.RECEIVER_DEFAULT_GROUP
    assert r.unresolvable_detail == (
        "Receiver U_RCV pin IN matched no pin group; default-group limits are not "
        "evidence for this pin")


def test_code_receiver_specs_unresolved():
    ir = _ir(_ic("U_DRV", "OUT", "VCC_5V"), _ic("U_RCV", "IN", "VCC_3V3"))
    [r] = _sig_rows(ir, {("U_DRV", "OUT"): _spec("OUT", "output"),
                         ("U_RCV", "IN"): _spec("IN", "input", notes=["N1", "N2"])})
    assert r.unresolvable_reason is R.RECEIVER_SPECS_UNRESOLVED
    assert r.unresolvable_detail == "Receiver specs unresolved — U_RCV: N1; N2"


def test_code_expr_notes_no_driver_voltage():
    ir = _ir(_ic("U_DRV", "OUT", None), _ic("U_RCV", "IN", None))
    [r] = _sig_rows(ir, {("U_DRV", "OUT"): _spec("OUT", "output"),
                         ("U_RCV", "IN"): _spec("IN", "input", notes=["N1"])})
    assert r.unresolvable_reason is R.NO_DRIVER_VOLTAGE_NO_RECEIVER_SPECS
    assert r.unresolvable_detail == (
        "Neither driver voltage nor receiver pin specs could be determined — "
        "receiver specs unresolved: N1")


def test_enum_has_exactly_ten_members_value_equals_name():
    assert [m.name for m in R] == [
        "NO_DRIVER_VOLTAGE_NO_RECEIVER_SPECS", "DRIVER_NO_DATASHEET",
        "DRIVER_NO_CONFIRMED_RAIL", "DRIVER_DOMAIN_AMBIGUOUS",
        "RECEIVER_SPECS_NOT_EXTRACTED", "DRIVER_NOT_PUSH_PULL",
        "RECEIVER_DEFAULT_GROUP", "RECEIVER_SPECS_UNRESOLVED", "NO_DRIVER_ON_NET",
        "MERGED_REFDES"]
    assert all(m.value == m.name for m in R)


# ── build_report ──────────────────────────────────────────────────────────────

def test_build_report_signal_row_shape(tmp_path):
    ir = _ir(_ic("U1", "IN", "VCC_5V"), _ic("U2", "IN", "VCC_3V3"))
    specs = {("U1", "IN"): _spec("IN", "input"), ("U2", "IN"): _spec("IN", "input")}
    results = run(ir, specs, CONFIRMED)
    ir2 = _ir(_ic("U_DRV", "OUT", "VCC_5V"), _ic("U_RCV", "IN", "VCC_3V3"))
    results += run(ir2, {("U_DRV", "OUT"): _spec("OUT", "output"),
                         ("U_RCV", "IN"): _spec("IN", "input", abs_max=6.0)}, CONFIRMED)
    rep = build_report(source_netlist="", results=results, confirmed_voltages=CONFIRMED,
                       extraction_metadata={}, output_path=str(tmp_path / "r.json"),
                       components=ir.components + ir2.components)
    on_disk = json.loads((tmp_path / "r.json").read_text())
    for rows in (rep["results"], on_disk["results"]):
        for e in rows:
            assert list(e["driver"]) == ["refdes", "part_number", "pin_name", "selection",
                                         "voltage_v", "voltage_source", "confidence"]
        nd = [e for e in rows if e["unresolvable_reason"] == "NO_DRIVER_ON_NET"]
        assert len(nd) == 2
        for e in nd:
            assert e["driver"]["refdes"] is None and e["driver"]["pin_name"] is None
            assert e["driver"]["selection"] == "none"
            assert e["explanation"] == NO_DRIVER_DETAIL
            assert e["explanation_source"] == "canned"
        [p] = [e for e in rows if e["status"] == "PASS"]
        assert p["unresolvable_reason"] is None
        assert p["driver"]["selection"] == "explicit_output" and p["driver"]["pin_name"] == "OUT"


# ── console ───────────────────────────────────────────────────────────────────

def test_unresolvable_line_signal_leads_with_explanation():
    r = {"driver": {"refdes": None}, "unresolvable_reason": "NO_DRIVER_ON_NET",
         "explanation": NO_DRIVER_DETAIL, "evidence_label": NO_DRIVER_LABEL}
    assert rct._unresolvable_line(r) == f"{NO_DRIVER_DETAIL} [{NO_DRIVER_LABEL}]"
    r_no_expl = dict(r, explanation=None)
    assert rct._unresolvable_line(r_no_expl) == f"NO_DRIVER_ON_NET [{NO_DRIVER_LABEL}]"


def test_unresolvable_line_supply_unchanged():
    r = {"unresolvable_reason": "NO_RATED_CEILING", "explanation": "some text",
         "evidence_label": "L"}
    assert rct._unresolvable_line(r) == "NO_RATED_CEILING [L]"
    assert rct._unresolvable_line({"evidence": " E "}) == "E"
