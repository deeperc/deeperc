"""TODO-528: KiCad subscript braces (V_{IN}) in step_08b's supply-pin admission.

D1: after the group listing finds nothing, a brace pin whose brace-deleted name passes
_is_supply_pin is admitted (True). Listing keeps precedence. _is_supply_pin itself is raw.
D2: for brace pins only, _find_matching_supply_group Pass 1 also tries the unbraced rail
name. Synthetic IR and groups only (never the real caches).
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps.step_02_parser import ComponentIR, PinIR
from steps.step_08b_supply_checker import (
    _admit_supply_pin,
    _find_matching_supply_group,
    _is_supply_pin,
    check_component_supplies,
)
from tests.test_todo484_supply_admission import AMS1117_GROUPS, R7FA4M1_GROUPS


def _comp(pin_name, pin_id="1", refdes="U1", net="RAIL"):
    return ComponentIR(refdes=refdes, part_number="X-PART", value="X-PART",
                       pins=[PinIR(pin_id=pin_id, pin_name=pin_name, net=net)])


def _pin(comp):
    return comp.pins[0]


def _grp(name, rail, smin, smax, absmax=None):
    return {"group_name": name, "pin_type": "power", "example_pins": [], "applies_to": "",
            "VIH_min": None, "VIL_max": None, "VOH_min": None, "VOL_max": None,
            "absolute_max_voltage": absmax, "supply_min": smin, "supply_max": smax,
            "supply_abs_max": absmax, "supply_rail_name": rail,
            "VIH_source": None, "abs_max_source": None}


# (1) brace supply names admitted by name, no groups
def test_brace_names_admitted_without_groups():
    for n in ("V_{IN}", "V_{DD}", "V_{CC}", "V_{CCIO}", "VCC_{A}", "VCC_{B}"):
        c = _comp(n)
        assert _admit_supply_pin(c, _pin(c), None) is True, n


# (2) the raw gate is unchanged
def test_is_supply_pin_still_raw():
    assert _is_supply_pin("V_{IN}") is False
    assert _is_supply_pin("VIN") is True


# (3) a listing group keeps precedence over the new name step
def test_listing_precedence():
    c = _comp("V_{IN}", pin_id="3")
    got = _admit_supply_pin(c, _pin(c), AMS1117_GROUPS)
    assert isinstance(got, list) and len(got) == 1
    assert got[0]["supply_rail_name"] == "VIN"
    c = _comp("VCC_{USB}", pin_id="30")
    got = _admit_supply_pin(c, _pin(c), R7FA4M1_GROUPS)
    assert isinstance(got, list) and len(got) == 1
    assert got[0]["supply_rail_name"] == "VCC_USB"


# (4) negatives stay []
def test_negatives_stay_empty():
    for n in ("V_{EN}", "VCC_{EN}", "V_{OK}", "~{VCC}", "{}", "", None, "GND_{A}", "P_{GND}"):
        c = _comp(n)
        assert _admit_supply_pin(c, _pin(c), None) == [], n
        assert _admit_supply_pin(c, _pin(c), AMS1117_GROUPS) == [], n


def test_inventory_call_shape_comp_none():
    assert _admit_supply_pin(None, SimpleNamespace(pin_name="V_{IN}"), None) is True
    assert _admit_supply_pin(None, SimpleNamespace(pin_name="V_{EN}"), None) == []


# (5) D2: Pass 1 on the unbraced rail name, brace pins only
def test_d2_pass1_unbraced_rail_match():
    vin = _grp("Input", "VIN", 4.5, 17.0, 18.0)
    vdd = _grp("Core", "VDD", 3.0, 3.6, 4.0)
    groups = {"pin_groups": [vdd, vin]}
    assert _find_matching_supply_group("V_{IN}", groups, 3.3) is vin
    assert _find_matching_supply_group("V_{IN}", {"pin_groups": [vdd, _grp("I", "V_IN", 4.5, 17.0)]},
                                       3.3)["supply_rail_name"] == "V_IN"
    # brace-free V_IN is unchanged: no exact rail match -> Pass 2 voltage pick (the VDD group)
    assert _find_matching_supply_group("V_IN", groups, 3.3) is vdd


# (6) end to end through check_component_supplies
def test_end_to_end_with_and_without_groups():
    comp = _comp("V_{IN}", pin_id="3", net="N5V")
    groups = {"pin_groups": [_grp("Input", "VIN", 4.5, 17.0, 18.0)]}
    res = check_component_supplies([comp], {comp.part_number: groups}, {"N5V": 5.0}, {})
    assert len(res) == 1
    r = res[0]
    assert r.supply_pin_name == "V_{IN}" and r.status == "PASS"
    assert (r.rated_min, r.rated_max, r.rated_abs_max) == (4.5, 17.0, 18.0)

    res = check_component_supplies([comp], {}, {"N5V": 5.0}, {})
    assert len(res) == 1 and res[0].status == "UNRESOLVABLE"
    assert res[0].unresolvable_reason.name == "SPEC_NOT_EXTRACTED"
