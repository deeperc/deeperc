"""TODO-554 — the signal check (step_08) stops treating non-IC parts as IC endpoints.

Covers part_class.classify_part (rules A..M), the step_02 libsource_description
field, the "?" refdes rule, the GAP-part treatment in step_08_checker.run, the
not_checked_out disclosure, and the report's not_checked_parts key.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps.part_class import NON_IC_CLASSES, classify_part  # noqa: E402
from steps.step_02_parser import (  # noqa: E402
    ComponentIR, NetIR, NetlistIR, PinIR, parse_netlist,
)
from steps.step_05_validator import ValidatedPinSpec  # noqa: E402
from steps.step_08_checker import is_passive_component, run  # noqa: E402
from steps.step_10_report import build_report  # noqa: E402


def _c(refdes="U9", footprint="", lib="", part="", desc=""):
    return SimpleNamespace(refdes=refdes, footprint=footprint, libsource_lib=lib,
                           libsource_part=part, libsource_description=desc)


# ── classify_part ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("comp, expected", [
    # A provenance (pseudo_part_provenance)
    (_c(refdes="TP1"), ("testpoint", "provenance")),
    (_c(refdes="J3"), ("connector", "provenance")),
    # B connector
    (_c(lib="Connector_Generic", part="XYZ"), ("connector", "library")),
    (_c(footprint="Connector_PinHeader_2.54mm:PinHeader_1x04"), ("connector", "footprint")),
    (_c(part="Conn_01x02"), ("connector", "part")),
    (_c(desc="Generic connector, single row"), ("connector", "description")),
    (_c(lib="BatteryHolder_Foo"), ("connector", "library")),
    # C testpoint
    (_c(part="TestPad_1mm"), ("testpoint", "part")),
    (_c(footprint="Misc:TestPoint_Pad_D1.0mm"), ("testpoint", "footprint")),
    # D jumper
    (_c(lib="Jumper", part="XX"), ("jumper", "library")),
    (_c(footprint="Jumper:Foo"), ("jumper", "footprint")),
    # E resistor network
    (_c(footprint="Resistor_SMD:R_Array_Convex_4x0603"), ("resistor_network", "footprint")),
    (_c(part="R_Pack04"), ("resistor_network", "part")),
    # F led
    (_c(part="LED"), ("led", "part")),
    (_c(footprint="LED_SMD:LED_0603"), ("led", "footprint")),
    (_c(desc="Light emitting diode"), ("led", "description")),
    # G switch
    (_c(footprint="Button_Switch_SMD:SW_SPST_B3U"), ("switch", "footprint")),
    (_c(part="Button_X"), ("switch", "part")),
    (_c(desc="Tactile switch, 6mm"), ("switch", "description")),
    # H relay
    (_c(lib="Relay", part="G5V-1"), ("relay", "library")),
    # I varistor / pot
    (_c(part="Varistor"), ("varistor_pot", "part")),
    (_c(footprint="Potentiometer_THT:Pot"), ("varistor_pot", "footprint")),
    # J protection diode
    (_c(lib="Diode_TVS"), ("protection_diode", "library")),
    (_c(lib="Power_Protection_Diodes"), ("protection_diode", "library")),
    (_c(desc="Bidirectional TVS diode"), ("protection_diode", "description")),
    (_c(desc="Schottky barrier rectifier"), ("protection_diode", "description")),
    # K transistor
    (_c(lib="Transistor_FET"), ("transistor", "library")),
    (_c(part="AO3401_MOS+DIODE"), ("transistor", "part")),
    (_c(desc="Power MOSFET, N-channel"), ("transistor", "description")),
    # L switch (footprint library)
    (_c(footprint="Button_Switch_THT:Foo"), ("switch", "footprint")),
    (_c(footprint="Some_Switch_Lib:Foo"), ("switch", "footprint")),
    # M protection diode (footprint library)
    (_c(footprint="Diode_SMD:D_SMA", lib="Device", part="D"), ("protection_diode", "footprint")),
])
def test_classify_part_rules(comp, expected):
    assert classify_part(comp) == expected
    assert expected[0] in NON_IC_CLASSES


def test_classify_part_rule_order_earlier_rule_wins():
    # matches B (library) and F (part)
    assert classify_part(_c(lib="Connector_Generic", part="LED")) == ("connector", "library")
    # matches J (library tvs) and K (description power mosfet)
    assert classify_part(_c(lib="Diode_TVS", desc="power mosfet")) == ("protection_diode", "library")


def test_classify_part_evidence_is_first_true_condition_in_rule():
    # rule B: footprint condition precedes the description condition
    assert classify_part(_c(footprint="Connector_X:Y", desc="a connector")) == ("connector", "footprint")


def test_classify_part_ic_is_none():
    assert classify_part(_c(refdes="U1", lib="MCU_ST_STM32F1", part="STM32F103C8Tx",
                            footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm")) is None


def test_classify_part_oscillator_u5_is_none():
    assert classify_part(_c(refdes="U5", lib="Oscillator", part="ASE-xxxMHz",
                            footprint="Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm")) is None


def test_classify_part_missing_attributes_read_empty():
    assert classify_part(object()) is None
    assert classify_part(SimpleNamespace(refdes="U1", libsource_lib="Connector")) == ("connector", "library")


def test_non_ic_classes_has_thirteen_values():
    assert len(NON_IC_CLASSES) == 13


# ── parser field ──────────────────────────────────────────────────────────────

_NETLIST = """(export (version "E")
  (components
    (comp (ref "U1") (value "X") (footprint "F:G")
      (libsource (lib "L") (part "P") (description "component-level ignored")))
    (comp (ref "U2") (value "Y")
      (libsource (lib "L") (part "Q"))))
  (libparts
    (libpart (lib "L") (part "P") (description "first wins"))
    (libpart (lib "L") (part "P") (description "second"))
    (libpart (lib "L") (part "Q")))
  (nets
    (net (code "1") (name "N") (node (ref "U1") (pin "1")) (node (ref "U2") (pin "1")))))
"""


def test_parser_fills_libsource_description(tmp_path):
    p = tmp_path / "b.net"
    p.write_text(_NETLIST)
    ir = parse_netlist(p)
    by = {c.refdes: c for c in ir.components}
    assert by["U1"].libsource_description == "first wins"
    assert by["U2"].libsource_description == ""


def test_parser_without_libparts_leaves_empty(tmp_path):
    p = tmp_path / "b.net"
    p.write_text(_NETLIST.split("  (libparts")[0] + """
  (nets (net (code "1") (name "N") (node (ref "U1") (pin "1")))))
""")
    ir = parse_netlist(p)
    assert all(c.libsource_description == "" for c in ir.components)


# ── "?" refdes rule ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref", ["R?", "C?", "D?", "Q?", "J?", "TP?", "R1", "J1"])
def test_question_mark_refdes_passive(ref):
    assert is_passive_component(ref)


@pytest.mark.parametrize("ref", ["U?", "IC?", "U1"])
def test_question_mark_refdes_not_passive(ref):
    assert not is_passive_component(ref)


# ── run(): GAP-part treatment ─────────────────────────────────────────────────

def _spec(part, pin, *, abs_max=None, VIH_min=None, VIH_max=None, pin_type="input"):
    return ValidatedPinSpec(
        part_number=part, pin_name=pin, pin_id="1", VIH_max=VIH_max, VIH_min=VIH_min,
        absolute_max_voltage=abs_max, pin_type=pin_type, source_snippet="snippet",
        signal_score=75, confidence="high", is_verified=True, pin_type_exact=True)


def _ic(refdes, sig_pins, vcc_net):
    pins = [PinIR(pin_id=str(i + 1), pin_name=name, net=net)
            for i, (name, net) in enumerate(sig_pins)]
    pins.append(PinIR(pin_id=str(len(pins) + 1), pin_name="VCC", net=vcc_net))
    return ComponentIR(refdes=refdes, part_number=refdes + "_PN", value="", pins=pins)


def _gap(refdes, sig_pins, vcc_net=None, lib="Connector_Generic"):
    c = _ic(refdes, sig_pins, vcc_net) if vcc_net else ComponentIR(
        refdes=refdes, part_number=refdes + "_PN", value="",
        pins=[PinIR(pin_id=str(i + 1), pin_name=n, net=net)
              for i, (n, net) in enumerate(sig_pins)])
    c.libsource_lib = lib
    return c


def _ir(components):
    nets: dict[str, list] = {}
    for c in components:
        for p in c.pins:
            nets.setdefault(p.net, []).append((c.refdes, p.pin_id))
    rails = [n for n in nets if n.startswith("VCC_")]
    return NetlistIR(source_file="t", components=components,
                     nets=[NetIR(name=n, pins=ps) for n, ps in nets.items()],
                     power_nets=rails, ground_nets=[])


CONFIRMED = {"VCC_3V3": 3.3, "VCC_5V": 5.0}


def test_gap_part_never_driver_despite_highest_rail_and_receiver_with_limits():
    drv = _ic("U1", [("PA0", "SIG")], "VCC_3V3")
    gap = _gap("U9", [("P1", "SIG")], vcc_net="VCC_5V")
    ir = _ir([drv, gap])
    specs = {("U9", "P1"): _spec("U9_PN", "P1", abs_max=5.5)}
    out = []
    rows = run(ir, specs, CONFIRMED, not_checked_out=out)
    sig = [r for r in rows if r.net_name == "SIG"]
    assert len(sig) == 1
    assert sig[0].driver_refdes == "U1"
    assert sig[0].receiver_refdes == "U9"
    assert sig[0].status == "PASS"
    assert out == []  # its only signal pin is the receiver of a row


def test_gap_pin_without_limits_gets_no_row():
    drv = _ic("U1", [("PA0", "SIG")], "VCC_3V3")
    rcv = _ic("U2", [("IN", "SIG")], "VCC_3V3")
    gap = _gap("U9", [("P1", "SIG")], vcc_net="VCC_5V")
    ir = _ir([drv, rcv, gap])
    rows = run(ir, {}, CONFIRMED)
    assert [r.receiver_refdes for r in rows if r.net_name == "SIG"] == ["U2"]
    assert all(r.driver_refdes != "U9" for r in rows)


def test_gap_only_net_emits_no_row_and_does_not_raise():
    a = _gap("U9", [("P1", "SIG")], vcc_net="VCC_5V")
    b = _gap("U8", [("P1", "SIG")], vcc_net="VCC_3V3")
    ir = _ir([a, b])
    specs = {("U9", "P1"): _spec("U9_PN", "P1", abs_max=5.5)}
    out = []
    assert [r for r in run(ir, specs, CONFIRMED, not_checked_out=out) if r.net_name == "SIG"] == []
    assert out == []  # no IC endpoint on SIG


def test_regex_passive_part_untouched_and_plain_passive_no_entry():
    drv = _ic("U1", [("PA0", "SIG")], "VCC_3V3")
    rcv = _ic("U2", [("IN", "SIG")], "VCC_3V3")
    r1 = ComponentIR(refdes="R1", part_number="10k", value="10k",
                     pins=[PinIR("1", "1", "SIG"), PinIR("2", "2", "VCC_3V3")],
                     libsource_lib="Device", libsource_part="R")
    ir = _ir([drv, rcv, r1])
    out = []
    rows = run(ir, {}, CONFIRMED, not_checked_out=out)
    assert [(r.driver_refdes, r.receiver_refdes) for r in rows] == [("U1", "U2")]
    assert out == []


def test_two_unannotated_relays_both_non_endpoints_two_entries():
    drv = _ic("U1", [("PA0", "SIG")], "VCC_3V3")
    rcv = _ic("U2", [("IN", "SIG")], "VCC_3V3")
    k1 = _gap("K?", [("A", "SIG")], lib="Relay_THT")
    k2 = _gap("K?", [("A", "SIG")], lib="Relay_SMD")
    ir = _ir([drv, rcv, k1, k2])
    out = []
    rows = run(ir, {}, CONFIRMED, not_checked_out=out)
    assert [(r.driver_refdes, r.receiver_refdes) for r in rows] == [("U1", "U2")]
    assert len(out) == 2
    assert out[0]["nets"] == out[1]["nets"] == ["SIG"]
    assert out[0]["part_class"] == out[1]["part_class"] == "relay"


def test_refdes_shared_with_unrecognised_component_is_not_recognised():
    drv = _ic("U1", [("PA0", "SIG")], "VCC_3V3")
    a = _gap("K?", [("A", "SIG")], lib="Relay_THT")
    b = ComponentIR(refdes="K?", part_number="X", value="", pins=[PinIR("1", "A", "SIG")],
                    libsource_lib="MCU_Foo")
    ir = _ir([drv, a, b])
    out = []
    rows = run(ir, {}, CONFIRMED, not_checked_out=out)
    assert out == []
    assert any(r.receiver_refdes == "K?" for r in rows)  # treated as an IC, as before


# ── not_checked_out disclosure ────────────────────────────────────────────────

def test_not_checked_entry_keys_order_sort_and_pins_with_rows():
    drv = _ic("U1", [("PA0", "SIG"), ("PA1", "SIG2")], "VCC_3V3")
    gap = _gap("U9", [("P1", "SIG"), ("P2", "SIG2")], vcc_net="VCC_5V")
    j1 = _gap("J1", [("1", "SIG2")])
    ir = _ir([drv, gap, j1])
    specs = {("U9", "P1"): _spec("U9_PN", "P1", abs_max=5.5)}
    out = [{"pre": "existing"}]
    run(ir, specs, CONFIRMED, not_checked_out=out)
    assert out[0] == {"pre": "existing"}
    entries = out[1:]
    assert [e["refdes"] for e in entries] == ["J1", "U9"]
    assert list(entries[0].keys()) == ["refdes", "part_number", "part_class", "class_evidence",
                                       "checker", "reason", "nets", "pins_with_rows"]
    j, u = entries
    assert j == {"refdes": "J1", "part_number": "J1_PN", "part_class": "connector",
                 "class_evidence": "provenance", "checker": "signal", "reason": "NON_IC_PART",
                 "nets": ["SIG2"], "pins_with_rows": 0}
    assert u["class_evidence"] == "library"
    assert u["nets"] == ["SIG2"]
    assert u["pins_with_rows"] == 1


def test_no_entry_for_part_whose_pins_are_on_nets_without_ic_endpoint():
    j1 = _gap("J1", [("1", "SIG")])
    r1 = ComponentIR(refdes="R1", part_number="10k", value="", pins=[PinIR("1", "1", "SIG")])
    drv = _ic("U1", [("PA0", "OTHER")], "VCC_3V3")
    ir = _ir([j1, r1, drv])
    out = []
    run(ir, {}, CONFIRMED, not_checked_out=out)
    assert out == []


def test_not_checked_out_none_same_rows():
    drv = _ic("U1", [("PA0", "SIG")], "VCC_3V3")
    gap = _gap("U9", [("P1", "SIG")], vcc_net="VCC_5V")
    rcv = _ic("U2", [("IN", "SIG")], "VCC_3V3")
    ir = _ir([drv, gap, rcv])
    specs = {("U9", "P1"): _spec("U9_PN", "P1", VIH_min=2.0)}
    assert run(ir, specs, CONFIRMED) == run(ir, specs, CONFIRMED, not_checked_out=[])


# ── build_report ──────────────────────────────────────────────────────────────

def test_build_report_not_checked_parts_default_and_count(tmp_path):
    rep = build_report(source_netlist="", results=[], confirmed_voltages={},
                       extraction_metadata={}, output_path=str(tmp_path / "a.json"))
    assert rep["not_checked_parts"] == []
    assert rep["summary"]["not_checked_parts"] == 0
    entry = {"refdes": "J1", "part_number": "X", "part_class": "connector",
             "class_evidence": "provenance", "checker": "signal", "reason": "NON_IC_PART",
             "nets": ["SIG"], "pins_with_rows": 0}
    rep = build_report(source_netlist="", results=[], confirmed_voltages={},
                       extraction_metadata={}, output_path=str(tmp_path / "b.json"),
                       not_checked_parts=[entry])
    assert rep["not_checked_parts"] == [entry]
    assert rep["summary"]["not_checked_parts"] == 1
