"""Unit tests for net-voltage rail propagation (Change B) and the
sibling-rail-consistency core-vs-IO mismatch guard (RP2040 Mode-0 safety).

No network. Constructs synthetic components + pin_groups and drives
step_08b_supply_checker.check_component_supplies directly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import step_08b_supply_checker as sc  # noqa: E402
from steps.step_02_parser import ComponentIR, PinIR  # noqa: E402


def _comp(refdes, pn, pins):
    return ComponentIR(refdes=refdes, part_number=pn, value=pn,
                       pins=[PinIR(pin_id=pid, pin_name=nm, net=net) for pid, nm, net in pins])


def _groups(*specs):
    return {"pin_groups": [
        {"group_name": g, "pin_type": "power", "example_pins": [g],
         "supply_rail_name": rail, "supply_min": lo, "supply_max": hi, "supply_abs_max": amax}
        for (g, rail, lo, hi, amax) in specs
    ]}


def _by_pin(results):
    return {(r.supply_pin_name): r for r in results}


# ── Sibling-rail-consistency guard (the RP2040 DVDD core-vs-IO case) ───────────

def test_core_vs_io_mismatch_becomes_unresolvable_not_warn():
    # Multi-rail part: DVDD on a dedicated 1.1V rail, IOVDD on 3.3V. The only
    # matched group [1.8,3.6] actually describes the IO sibling rail, so DVDD's
    # 1.1V must NOT read as a genuine undervoltage WARN.
    comp = _comp("U1", "MCUX", [
        ("23", "DVDD", "+1V1"), ("1", "IOVDD", "+3V3"),
    ])
    groups = _groups(("VDD", "VDD", 1.8, 3.6, 4.6))
    res = _by_pin(sc.check_component_supplies(
        [comp], {"MCUX": groups}, {"+1V1": 1.1, "+3V3": 3.3}))
    assert res["DVDD"].status == "UNRESOLVABLE"
    assert "core-vs-IO" in res["DVDD"].evidence_label
    # IOVDD on the matching rail still resolves normally.
    assert res["IOVDD"].status == "PASS"


def test_genuine_undervoltage_single_rail_stays_warn():
    # Single-rail part fed slightly below its min — NO sibling rail inside the
    # group's range, so the guard must NOT fire; it stays a real WARN.
    comp = _comp("U1", "PARTX", [("1", "VCC", "+3V0"), ("2", "VCC", "+3V0")])
    groups = _groups(("VCC", "VCC", 3.135, 3.465, 4.0))
    res = _by_pin(sc.check_component_supplies(
        [comp], {"PARTX": groups}, {"+3V0": 3.0}))
    assert res["VCC"].status == "WARN"
    assert "core-vs-IO" not in (res["VCC"].evidence_label or "")


# ── Passive-bridge provenance grading ─────────────────────────────────────────

def test_high_confidence_propagation_passes_with_note():
    comp = _comp("U1", "AMCU", [("24", "AVCC", "AVCC")])
    groups = _groups(("AVCC Supply", "AVCC", 2.7, 5.5, 6.0))
    prov = {"AVCC": {"confidence": "high", "rail": "+5V", "voltage": 5.0,
                     "bridge": "FB1", "bridge_type": "inductor"}}
    res = _by_pin(sc.check_component_supplies(
        [comp], {"AMCU": groups}, {"AVCC": 5.0}, derived_provenance=prov))
    assert res["AVCC"].status == "PASS"
    assert "propagated from" in res["AVCC"].evidence_label


def test_medium_confidence_diode_downgrades_pass_to_warn():
    comp = _comp("U1", "FLASHX", [("8", "VCC", "FLASH_PWR")])
    groups = _groups(("VCC", "VCC", 2.7, 3.6, 4.0))
    prov = {"FLASH_PWR": {"confidence": "medium", "rail": "+3V3", "voltage": 3.3,
                          "bridge": "D15", "bridge_type": "diode"}}
    res = _by_pin(sc.check_component_supplies(
        [comp], {"FLASHX": groups}, {"FLASH_PWR": 3.3}, derived_provenance=prov))
    assert res["VCC"].status == "WARN"
    assert "assumed" in res["VCC"].evidence_label.lower()


def test_no_provenance_is_unchanged():
    comp = _comp("U1", "PARTY", [("1", "VCC", "+3V3")])
    groups = _groups(("VCC", "VCC", 2.7, 3.6, 4.0))
    res = _by_pin(sc.check_component_supplies(
        [comp], {"PARTY": groups}, {"+3V3": 3.3}))
    assert res["VCC"].status == "PASS"
    assert "propagated" not in (res["VCC"].evidence_label or "")
