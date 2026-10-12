"""TODO-484 A-prime supply-pin admission, R1 PASS label, TODO-200 ordering rule.

On a part with extracted pin groups, a pin the supply NAME gate rejects is checked
iff a pin_type:power group lists it in example_pins (pin id or name) AND that group
carries supply_max or supply_abs_max; it binds to that listing group. Passive refdes,
pseudo-parts and ground/pad pin names are never admitted this way. In-memory IR only
(no pipeline, no cache read).
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import provenance
from steps.part_class import is_ground_or_pad_pin
from steps.step_02_parser import ComponentIR, PinIR
from steps.step_05_validator import validate_supply_group
from steps.step_08b_supply_checker import (
    SupplyUnresolvableReason,
    _find_matching_supply_group,
    check_component_supplies,
)
from steps.step_08c_structural_checker import GROUND_PIN_NAMES


# Copied from the tracked cache
# datasheets_parsed/5272_ams1117-3.3/auto/5272_ams1117-3.3_pin_groups.json
AMS1117_GROUPS = {"pin_groups": [
    {"group_name": "Input Supply Voltage (VIN)", "pin_type": "power", "example_pins": ["3"],
     "applies_to": "VIN pin - positive input supply", "VIH_min": None, "VIL_max": None,
     "VOH_min": None, "VOL_max": None, "absolute_max_voltage": 18.0, "supply_min": None,
     "supply_max": 12.0, "supply_abs_max": 18.0, "supply_rail_name": "VIN",
     "VIH_source": None, "abs_max_source": "Absolute Maximum Ratings"},
    {"group_name": "Ground/Adjust Pin", "pin_type": "power", "example_pins": ["1"],
     "applies_to": "GND/ADJ pin - ground or adjustment terminal", "VIH_min": None,
     "VIL_max": None, "VOH_min": None, "VOL_max": None, "absolute_max_voltage": None,
     "supply_min": None, "supply_max": None, "supply_abs_max": None,
     "supply_rail_name": None, "VIH_source": None, "abs_max_source": None},
    {"group_name": "Output Voltage (VOUT)", "pin_type": "digital_output", "example_pins": ["2"],
     "applies_to": "VOUT pin - regulated output voltage", "VIH_min": None, "VIL_max": None,
     "VOH_min": 3.235, "VOL_max": None, "absolute_max_voltage": None, "supply_min": None,
     "supply_max": None, "supply_abs_max": None, "supply_rail_name": None,
     "VIH_source": None, "abs_max_source": None},
]}

# Copied from the tracked cache
# datasheets_parsed/r7fa4m1ab3cfm_aa0/auto/r7fa4m1ab3cfm_aa0_pin_groups.json (VCC, VCC_USB)
R7FA4M1_GROUPS = {"pin_groups": [
    {"group_name": "VCC Power Supply", "pin_type": "power", "example_pins": ["VCC"],
     "applies_to": "Main power supply pins", "VIH_min": None, "VIL_max": None,
     "VOH_min": None, "VOL_max": None, "absolute_max_voltage": 6.5, "supply_min": 1.6,
     "supply_max": 5.5, "supply_abs_max": 6.5, "supply_rail_name": "VCC",
     "VIH_source": None, "abs_max_source": "Table 2.1 Absolute maximum ratings"},
    {"group_name": "VCC_USB Power Supply", "pin_type": "power", "example_pins": ["VCC_USB"],
     "applies_to": "USB power supply pins", "VIH_min": None, "VIL_max": None,
     "VOH_min": None, "VOL_max": None, "absolute_max_voltage": 6.5, "supply_min": 3.0,
     "supply_max": 3.6, "supply_abs_max": 6.5, "supply_rail_name": "VCC_USB",
     "VIH_source": None, "abs_max_source": "Table 2.1 Absolute maximum ratings"},
]}


def _ams1117(refdes="U1", **kw):
    return ComponentIR(refdes=refdes, part_number="AMS1117-3.3", value="AMS1117-3.3", pins=[
        PinIR(pin_id="1", pin_name="GND", net="GND"),
        PinIR(pin_id="2", pin_name="VO", net="+3V3"),
        PinIR(pin_id="3", pin_name="VI", net="VIN_RAIL"),
    ], **kw)


def _run(comp, groups, volts):
    return check_component_supplies([comp], {comp.part_number: groups}, volts, {})


# ── T1 AMS1117 VI (pin 3) ─────────────────────────────────────────────────────

def test_t1_ams1117_vi_pass_max_only_label():
    res = _run(_ams1117(), AMS1117_GROUPS, {"VIN_RAIL": 3.3, "+3V3": 3.3})
    assert [r.supply_pin_id for r in res] == ["3"]
    r = res[0]
    assert r.status == "PASS"
    assert r.evidence_label == ("Confirmed — supply 3.3V within rated max 12.0V "
                                "(datasheet states no rated minimum)")
    assert r.rated_min is None and r.rated_max == 12.0
    assert r.confidence == "medium"


def test_t1_ams1117_vi_over_rated_max_fails():
    res = _run(_ams1117(), AMS1117_GROUPS, {"VIN_RAIL": 13.0})
    assert len(res) == 1 and res[0].status == "FAIL"
    assert "exceeds rated supply max 12.0V" in res[0].evidence_label


def test_t1_ams1117_vi_over_abs_max_fails():
    res = _run(_ams1117(), AMS1117_GROUPS, {"VIN_RAIL": 18.5})
    assert len(res) == 1 and res[0].status == "FAIL"
    assert "exceeds supply absolute max 18.0V" in res[0].evidence_label


def test_t1_ams1117_vi_no_net_voltage_unresolvable():
    res = _run(_ams1117(), AMS1117_GROUPS, {})
    assert len(res) == 1 and res[0].supply_pin_id == "3"
    assert res[0].status == "UNRESOLVABLE"
    assert res[0].unresolvable_reason is SupplyUnresolvableReason.NET_VOLTAGE_NOT_CONFIRMED


# ── T2 UNO R4 shape: VCC_USB bound to its own listing group ──────────────────

def _r7fa4m1():
    return ComponentIR(refdes="U3", part_number="R7FA4M1AB3CFM#AA0", value="R7FA4M1", pins=[
        PinIR(pin_id="11", pin_name="VCC", net="+3V3"),
        PinIR(pin_id="30", pin_name="VCC_USB", net="USB_RAIL"),
    ])


def test_t2_vcc_usb_pass_within_3v0_3v6():
    res = {r.supply_pin_name: r for r in _run(_r7fa4m1(), R7FA4M1_GROUPS,
                                               {"+3V3": 3.3, "USB_RAIL": 3.3})}
    r = res["VCC_USB"]
    assert (r.rated_min, r.rated_max, r.status) == (3.0, 3.6, "PASS")


def test_t2_vcc_usb_5v_fails():
    res = {r.supply_pin_name: r for r in _run(_r7fa4m1(), R7FA4M1_GROUPS,
                                               {"+3V3": 3.3, "USB_RAIL": 5.0})}
    r = res["VCC_USB"]
    assert (r.rated_min, r.rated_max, r.status) == (3.0, 3.6, "FAIL")


# ── T3 exclusions ─────────────────────────────────────────────────────────────

def test_t3_passive_refdes_not_admitted():
    for refdes in ("R1", "C3", "J2", "Q1"):
        assert _run(_ams1117(refdes=refdes), AMS1117_GROUPS, {"VIN_RAIL": 3.3}) == []


def test_t3_pseudo_part_not_admitted():
    comp = _ams1117(refdes="U5", libsource_part="TestPoint")
    assert _run(comp, AMS1117_GROUPS, {"VIN_RAIL": 3.3}) == []


def test_t3_ground_pad_names_not_admitted():
    for name in ("P_{GND}", "A_{GND}", "GND_EP", "GND_B1", "UGND", "GNDPAD", "EP", "VSSA"):
        groups = copy.deepcopy(AMS1117_GROUPS)
        groups["pin_groups"][0]["example_pins"] = [name]
        comp = ComponentIR(refdes="U1", part_number="AMS1117-3.3", value="",
                           pins=[PinIR(pin_id="9", pin_name=name, net="N")])
        assert _run(comp, groups, {"N": 3.3}) == [], name


# ── T4 boundless listing group ────────────────────────────────────────────────

def test_t4_boundless_listing_group_not_admitted():
    groups = copy.deepcopy(AMS1117_GROUPS)
    g = groups["pin_groups"][0]
    g["supply_min"] = g["supply_max"] = g["supply_abs_max"] = None
    assert _run(_ams1117(), groups, {"VIN_RAIL": 3.3}) == []


# ── T5 explicit null example_pins ─────────────────────────────────────────────

def test_t5_null_example_pins_no_exception():
    groups = copy.deepcopy(AMS1117_GROUPS)
    for g in groups["pin_groups"]:
        g["example_pins"] = None
    assert _run(_ams1117(), groups, {"VIN_RAIL": 3.3}) == []


# ── T6 name-admitted pin keeps the Pass 1 / Pass 2 path ───────────────────────

def test_t6_name_admitted_pin_uses_pass2_unchanged():
    groups = {"pin_groups": [
        {"group_name": "IO supply", "pin_type": "power", "example_pins": ["5"],
         "supply_min": 1.8, "supply_max": 3.6, "supply_abs_max": 4.0,
         "supply_rail_name": "VDDIO"},
    ]}
    comp = ComponentIR(refdes="U7", part_number="X1", value="", pins=[
        PinIR(pin_id="4", pin_name="VCC", net="+3V3"),
    ])
    res = _run(comp, groups, {"+3V3": 3.3})
    expected = _find_matching_supply_group("VCC", groups, 3.3)
    assert expected is groups["pin_groups"][0]
    assert len(res) == 1
    r = res[0]
    assert (r.rated_min, r.rated_max, r.rated_abs_max) == (
        expected["supply_min"], expected["supply_max"], expected["supply_abs_max"])
    assert r.status == "PASS"
    assert r.evidence_label == "Confirmed — supply 3.3V within rated range 1.8V - 3.6V"


# ── T7 is_ground_or_pad_pin ───────────────────────────────────────────────────

def test_t7_ground_pin_names_all_true():
    assert len(GROUND_PIN_NAMES) == 12
    assert {n: is_ground_or_pad_pin(n) for n in GROUND_PIN_NAMES} == \
        {n: True for n in GROUND_PIN_NAMES}


def test_t7_supply_names_false():
    names = ("VI", "VIN", "VCC_USB", "VLOGIC", "BIAS", "VO", "SW", "VDD33")
    assert {n: is_ground_or_pad_pin(n) for n in names} == {n: False for n in names}


def test_t7_empty_false():
    assert is_ground_or_pad_pin("") is False


# ── T8 validate_supply_group ordering rule ───────────────────────────────────

def test_t8_abs_below_max_rejected():
    v = validate_supply_group({"supply_min": 1.0, "supply_max": 5.0, "supply_abs_max": 4.0})
    assert (v.ok, v.reason) == (False, "supply_abs_max_below_supply_max")


def test_t8_abs_below_max_rejected_without_min():
    v = validate_supply_group({"supply_min": None, "supply_max": 5.0, "supply_abs_max": 4.0})
    assert (v.ok, v.reason) == (False, "supply_abs_max_below_supply_max")


def test_t8_negative_rail_skipped():
    v = validate_supply_group({"supply_min": -36, "supply_max": -4.5, "supply_abs_max": -40})
    assert v.ok is True


def test_t8_rule1_wins():
    v = validate_supply_group({"supply_min": 3.3, "supply_max": 3.3, "supply_abs_max": 2.0})
    assert (v.ok, v.reason) == (False, "supply_min_equals_supply_max")


# ── T9 era membership ─────────────────────────────────────────────────────────

def test_t9_part_class_in_core():
    assert "steps/part_class.py" in provenance.CORE_PIPELINE_FILES
