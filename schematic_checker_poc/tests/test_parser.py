import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps.step_02_parser import parse, parse_netlist

NETLIST = os.path.join(os.path.dirname(__file__), "..", "test_netlist", "stm32_5v_violation.net")

REAL_FORMAT_NET = """
(export (version "E")
  (components
    (comp (ref "U1")
      (value "STM32F103C8T6")
      (fields
        (field (name "MPN") "STM32F103C8T6"))
      (pins
        (pin (num "10") (name "PA0") (type "bidirectional"))
        (pin (num "23") (name "VDD") (type "power_in")))))
  (nets
    (net
      (code "1")
      (name "VCC_3V3")
      (node (ref "U1") (pin "23")))
    (net
      (code "2")
      (name "SIGNAL_QA")
      (node (ref "U1") (pin "10")))))
"""


def _parse_from_string(text: str):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".net", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        return parse_netlist(path)
    finally:
        os.unlink(path)


# ── Existing tests (hand-crafted format A) ────────────────────────────────────

def test_component_count():
    ir = parse(NETLIST)
    assert len(ir.components) == 3


def test_net_count():
    ir = parse(NETLIST)
    assert len(ir.nets) == 4


def test_u1_part_number():
    ir = parse(NETLIST)
    u1 = next(c for c in ir.components if c.refdes == "U1")
    assert u1.part_number == "STM32F103C8T6"


def test_signal_qa_pins():
    ir = parse(NETLIST)
    qa_net = next(n for n in ir.nets if n.name == "SIGNAL_QA")
    assert len(qa_net.pins) == 2
    assert ("U2", "15") in qa_net.pins
    assert ("U1", "10") in qa_net.pins


def test_vcc_5v_pins():
    ir = parse(NETLIST)
    net = next(n for n in ir.nets if n.name == "VCC_5V")
    assert ("U2", "16") in net.pins
    assert ("J1", "1") in net.pins


def test_malformed_raises():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".net", delete=False) as f:
        f.write("(not_an_export (garbage))")
        path = f.name
    with pytest.raises(ValueError):
        parse(path)


# ── New tests (real KiCad export format B) ────────────────────────────────────

def test_real_format_parses():
    ir = _parse_from_string(REAL_FORMAT_NET)
    assert len(ir.components) == 1
    assert ir.components[0].refdes == "U1"
    assert ir.components[0].part_number == "STM32F103C8T6"
    assert len(ir.nets) == 2
    net = next(n for n in ir.nets if n.name == "SIGNAL_QA")
    assert ("U1", "10") in net.pins


def test_real_format_pin_backfill():
    ir = _parse_from_string(REAL_FORMAT_NET)
    u1 = ir.components[0]
    pa0 = next(p for p in u1.pins if p.pin_id == "10")
    assert pa0.net == "SIGNAL_QA"
    vdd = next(p for p in u1.pins if p.pin_id == "23")
    assert vdd.net == "VCC_3V3"


def test_real_format_net_code_ignored():
    ir = _parse_from_string(REAL_FORMAT_NET)
    names = {n.name for n in ir.nets}
    assert "VCC_3V3" in names
    assert "SIGNAL_QA" in names


def test_pins_synthesized_from_nets_when_components_has_none():
    """
    Real KiCad exports omit pin listings in (comp ...) blocks.
    Parser must synthesize comp.pins from (nets ...) section.
    """
    REAL_FORMAT_NO_PINS = """
(export (version "E")
  (components
    (comp (ref "IC1")
      (value "SN74LVCH16T245DGG")
      (fields (field (name "MPN") "SN74LVCH16T245DGG"))))
  (nets
    (net (code "1") (name "VCC_3V3")
      (node (ref "IC1") (pin "VCCA")))
    (net (code "2") (name "GND")
      (node (ref "IC1") (pin "GND")))
    (net (code "3") (name "DATA_A1")
      (node (ref "IC1") (pin "A1")))))
"""
    ir = _parse_from_string(REAL_FORMAT_NO_PINS)
    assert len(ir.components) == 1
    comp = ir.components[0]
    assert len(comp.pins) == 3, f"Expected 3 synthesized pins, got {len(comp.pins)}"
    pin_nets = {p.pin_id: p.net for p in comp.pins}
    assert pin_nets["VCCA"] == "VCC_3V3"
    assert pin_nets["GND"] == "GND"
    assert pin_nets["A1"] == "DATA_A1"
    # pin_name should equal pin_id for synthesized pins
    pin_names = {p.pin_id: p.pin_name for p in comp.pins}
    assert pin_names["VCCA"] == "VCCA"
    assert pin_names["A1"] == "A1"


def test_pintype_parsed_parallel_to_pins():
    """(pintype ...) is threaded into NetlistIR.pintypes as a parallel
    (refdes, pin_id) -> etype dict — NetIR.pins stays a plain (ref, pin) tuple
    list, unaffected."""
    REAL_FORMAT_WITH_PINTYPE = """
(export (version "E")
  (components
    (comp (ref "U1")
      (value "AP22615A")
      (fields (field (name "MPN") "AP22615A"))))
  (nets
    (net (code "1") (name "VCC_3V3")
      (node (ref "U1") (pin "1") (pintype "power_in")))
    (net (code "2") (name "CSIA_FLG")
      (node (ref "U1") (pin "3") (pinfunction "FLG") (pintype "output")))))
"""
    ir = _parse_from_string(REAL_FORMAT_WITH_PINTYPE)
    assert ir.pintypes[("U1", "1")] == "power_in"
    assert ir.pintypes[("U1", "3")] == "output"
    # NetIR.pins tuple shape is unchanged
    net = next(n for n in ir.nets if n.name == "CSIA_FLG")
    assert net.pins == [("U1", "3")]


def test_pintypes_empty_when_field_absent():
    ir = parse(NETLIST)
    assert ir.pintypes == {}


def test_kicad_sch_raises_clearly():
    with pytest.raises(ValueError, match="schematic source"):
        parse_netlist("dummy.kicad_sch")


def test_sch_raises_clearly():
    with pytest.raises(ValueError, match="schematic source"):
        parse_netlist("dummy.sch")
