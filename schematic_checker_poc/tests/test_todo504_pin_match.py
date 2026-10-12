"""TODO-504 — spec-lookup matcher (steps/pin_match.py): S1 unchanged, S2/S3
normalized stages that never pick, null example_pins, step_08b listing, and the
step_08 receiver-fallback guard.

U1 reads the shipped caches listed in datasheets_parsed/CACHE_MANIFEST.json
(read-only; the TODO-320 tripwire watches writes).
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import provenance  # noqa: E402
from steps import pin_match, step_04b_extractor  # noqa: E402
from steps.pin_match import (  # noqa: E402
    listing_supply_groups, match_groups, norm, pin_match_meta, resolve_pin_spec_from_groups,
    split_entry,
)
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR  # noqa: E402
from steps.step_05_validator import ValidatedPinSpec  # noqa: E402
from steps.step_08_checker import run  # noqa: E402

POC = Path(__file__).resolve().parent.parent


# ── U1 reference: the pre-TODO-504 function body, verbatim (step_04b @ bc5c920) ──

def _old_resolve(pin_id, pin_name, pin_groups_result):
    if not pin_groups_result:
        return None

    groups = pin_groups_result.get("pin_groups", [])
    default_group_name = pin_groups_result.get("default_group")

    # Priority 1: exact match on pin_id or pin_name in example_pins
    for group in groups:
        examples = [str(p).upper() for p in group.get("example_pins", [])]
        if pin_id.upper() in examples or pin_name.upper() in examples:
            return group

    # Priority 2: default_group — but ONLY if it is not a power group
    if default_group_name:
        for group in groups:
            if (group.get("group_name") == default_group_name
                    and group.get("pin_type") != "power"):
                return group

    # Priority 3: first signal group — never return a power group as fallback
    SIGNAL_TYPES = {"bidirectional", "digital_input", "digital_output", "analog"}
    signal_groups = [g for g in groups
                     if g.get("pin_type", "").lower() in SIGNAL_TYPES]
    if signal_groups:
        return signal_groups[0]

    # Priority 4: absolute last resort — any non-power group
    non_power = [g for g in groups if g.get("pin_type") != "power"]
    if non_power:
        return non_power[0]

    return None


def _shipped_caches():
    manifest = json.loads((POC / "datasheets_parsed" / "CACHE_MANIFEST.json").read_text())
    return sorted(f for f in manifest["files"] if f.endswith("_pin_groups.json"))


def test_u1_s1_unchanged_on_every_shipped_example_pin():
    files = _shipped_caches()
    assert len(files) == 112
    checked, diffs = 0, []
    for rel in files:
        d = json.loads((POC / "datasheets_parsed" / rel).read_text())
        for g in d.get("pin_groups", []):
            for p in g.get("example_pins") or []:
                p = str(p)
                if split_entry(p)[0] != "bare":
                    # TODO-531: a "<num>|<name>" or "?|<name>" entry (TODO-488) is not a
                    # pre-TODO-504 token; _old_resolve matches the whole string literally,
                    # which no board pin carries. Those forms are pinned by
                    # test_todo488_pairs.py and test_todo531_binding.py.
                    continue
                for name in (p, ""):
                    checked += 1
                    if resolve_pin_spec_from_groups(p, name, d) is not _old_resolve(p, name, d):
                        diffs.append((rel, p, name))
    assert checked > 0, "no bare-token example pin left in the shipped caches: retire U1"
    assert diffs == []


# ── helpers ─────────────────────────────────────────────────────────────────

def _g(name, pins, pin_type="digital_input", **kw):
    return {"group_name": name, "pin_type": pin_type, "example_pins": pins, **kw}


def _res(*groups, default=None):
    return {"pin_groups": list(groups), "default_group": default}


# ── U2 S2 ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("board,example", [
    ("~{OE}", "OE"), ("1~{OE}", "1OE"), ("V_{IN}", "V_IN"),
])
def test_u2_norm_strips_kicad_markup(board, example):
    other = _g("Other", ["X1"])
    target = _g("Target", [example])
    r = _res(other, target, default="Other")
    assert match_groups("99", board, r["pin_groups"]) == ("norm", [target])
    assert resolve_pin_spec_from_groups("99", board, r) is target
    assert pin_match_meta("99", board, r)[0] is True


def test_u2_leading_slash_example_matches_overbar_board_name():
    target = _g("Chip select", ["/CS"])
    r = _res(_g("Other", ["X1"]), target, default="Other")
    assert norm("/CS") == norm("~{CS}") == "CS"
    assert resolve_pin_spec_from_groups("99", "~{CS}", r) is target
    assert pin_match_meta("99", "~{CS}", r)[0] is True


# ── U3 S2 no-pick ───────────────────────────────────────────────────────────

def test_u3_norm_hitting_two_groups_falls_back():
    a = _g("A", ["OE"])
    b = _g("B", ["~{OE}x", "/OE"])
    dflt = _g("Default", ["Z9"])
    r = _res(dflt, a, b, default="Default")
    assert match_groups("99", "~{OE}", r["pin_groups"]) == ("none", [])
    assert resolve_pin_spec_from_groups("99", "~{OE}", r) is dflt
    assert pin_match_meta("99", "~{OE}", r)[0] is False


# ── U4 S3 ───────────────────────────────────────────────────────────────────

def test_u4_slash_alternates_in_two_groups_no_match():
    dflt = _g("Default", ["Z9"])
    adc = _g("ADC", ["AN019"], pin_type="analog")
    gpio = _g("GPIO", ["P103"], pin_type="bidirectional")
    r = _res(dflt, adc, gpio, default="Default")
    assert match_groups("7", "AN019/P103", r["pin_groups"]) == ("none", [])
    assert resolve_pin_spec_from_groups("7", "AN019/P103", r) is dflt
    assert pin_match_meta("7", "AN019/P103", r)[0] is False


def test_u4_slash_single_listed_alternate_matches():
    dflt = _g("Default", ["Z9"])
    port = _g("Port A", ["PA9"], pin_type="bidirectional")
    r = _res(dflt, port, default="Default")
    assert match_groups("30", "PA9/TX", r["pin_groups"]) == ("slash", [port])
    assert resolve_pin_spec_from_groups("30", "PA9/TX", r) is port
    assert pin_match_meta("30", "PA9/TX", r)[0] is True


# ── U5 D4 null example_pins ─────────────────────────────────────────────────

def test_u5_null_or_missing_example_pins_never_raise_never_match():
    null_g = {"group_name": "Null", "pin_type": "digital_input", "example_pins": None}
    missing_g = {"group_name": "Missing", "pin_type": "digital_input"}
    listed = _g("Listed", ["PB1"])
    r = _res(null_g, missing_g, listed, default="Listed")
    for pid, name in (("1", "PB1"), ("1", "~{PB1}"), ("1", "PB1/X"), ("1", "NOPE")):
        kind, hits = match_groups(pid, name, r["pin_groups"])
        assert null_g not in hits and missing_g not in hits
    assert resolve_pin_spec_from_groups("1", "PB1", r) is listed
    # Fallback: the default group (listed) wins; null/missing groups are not picked.
    assert resolve_pin_spec_from_groups("1", "NOPE", r) is listed
    assert listing_supply_groups("1", "NOPE", r) == []


def test_u5_null_example_pins_treated_as_empty_in_fallback():
    # D4: null is treated as an empty list everywhere, so the unchanged P3 fallback
    # treats a null-example group exactly as it treats an empty-example group.
    null_g = {"group_name": "Null", "pin_type": "digital_input", "example_pins": None}
    empty_g = _g("Empty", [])
    assert resolve_pin_spec_from_groups("1", "NOPE", _res(null_g)) is null_g
    assert resolve_pin_spec_from_groups("1", "NOPE", _res(empty_g)) is empty_g


# ── U6 step_08b listing ─────────────────────────────────────────────────────

def _pwr(name, pins, **kw):
    kw.setdefault("supply_max", 3.6)
    return _g(name, pins, pin_type="power", **kw)


def test_u6_s1_power_with_ceiling_as_before():
    vdd = _pwr("VDD", ["VDD", "12"])
    nocap = _g("VDD no ceiling", ["VDD"], pin_type="power")
    sig = _g("Signal", ["VDD"])
    r = _res(vdd, nocap, sig)
    assert listing_supply_groups("12", "", r) == [vdd]
    assert listing_supply_groups("3", "vdd", r) == [vdd]


def test_u6_slash_hitting_two_power_groups_lists_both():
    vdda = _pwr("Analog supply", ["VDDA"])
    vref = _pwr("Reference", ["VREF+"], supply_max=None, supply_abs_max=4.0)
    r = _res(_g("GPIO", ["PA0"], pin_type="bidirectional"), vdda, vref)
    assert listing_supply_groups("9", "VDDA/VREF+", r) == [vdda, vref]


def test_u6_slash_hitting_power_and_analog_lists_none():
    vrefh = _pwr("Reference", ["VREFH"])
    adc = _g("ADC", ["AN007"], pin_type="analog")
    r = _res(vrefh, adc)
    assert listing_supply_groups("40", "VREFH/AN007/P012", r) == []


def test_u6_step08b_wrapper_delegates():
    from steps import step_08b_supply_checker as s8b
    vdda = _pwr("Analog supply", ["VDDA"])
    pin = PinIR(pin_id="9", pin_name="~{VDDA}", net="X")
    assert s8b._listing_supply_groups(pin, _res(vdda)) == [vdda]


# ── U7 step_08 receiver-fallback guard ──────────────────────────────────────

def _ir():
    ir = NetlistIR(
        source_file="test",
        components=[
            ComponentIR(refdes="U_DRV", part_number="Drv", value="Drv", pins=[
                PinIR(pin_id="1", pin_name="VCC", net="VCC_5V"),
                PinIR(pin_id="2", pin_name="IO", net="SIG"),
            ]),
            ComponentIR(refdes="U_RCV", part_number="Rcv", value="Rcv", pins=[
                PinIR(pin_id="1", pin_name="IN", net="SIG"),
                PinIR(pin_id="2", pin_name="VCC", net="VCC_3V3"),
            ]),
        ],
        nets=[
            NetIR(name="SIG", pins=[("U_DRV", "2"), ("U_RCV", "1")]),
            NetIR(name="VCC_5V", pins=[("U_DRV", "1")]),
            NetIR(name="VCC_3V3", pins=[("U_RCV", "2")]),
        ],
        power_nets=["VCC_5V", "VCC_3V3"],
        ground_nets=[],
    )
    return ir, {"VCC_5V": 5.0, "VCC_3V3": 3.3}


def _recv(abs_max, exact):
    return ValidatedPinSpec(
        part_number="Rcv", pin_name="IN", pin_id="1",
        VIH_max=None, VIH_min=None, absolute_max_voltage=abs_max, pin_type="input",
        source_snippet="abs max", signal_score=75, confidence="high",
        is_verified=True, pin_type_exact=exact,
    )


def _drv(pin_type, exact=True):
    return ValidatedPinSpec(
        part_number="Drv", pin_name="IO", pin_id="2",
        VIH_max=None, VIH_min=None, absolute_max_voltage=None, pin_type=pin_type,
        source_snippet=None, signal_score=5, confidence="low", is_verified=False,
        pin_type_exact=exact, open_drain=False,
    )


def _sig(recv, drv):
    ir, confirmed = _ir()
    specs = {("U_RCV", "IN"): recv, ("U_DRV", "IO"): drv}
    return next(r for r in run(ir, specs, confirmed) if r.net_name == "SIG")


def test_u7_fallback_receiver_fail_downgraded():
    r = _sig(_recv(3.6, exact=False), _drv("output"))
    assert r.status == "UNRESOLVABLE"
    assert r.unresolvable_reason == (
        "Receiver U_RCV pin IN matched no pin group; "
        "default-group limits are not evidence for this pin")
    assert r.combined_confidence == "low"
    assert "would-be FAIL" in r.evidence_label


def test_u7_exact_receiver_still_fails():
    r = _sig(_recv(3.6, exact=True), _drv("output"))
    assert r.status == "FAIL"
    assert r.driver_voltage == 5.0


def test_u7_fallback_receiver_pass_labelled():
    r = _sig(_recv(6.0, exact=False), _drv("output"))
    assert r.status == "PASS"
    assert r.evidence_label.endswith(
        "(receiver limits from the part's default pin group; pin not listed)")


def test_u7_driver_guard_takes_precedence():
    r = _sig(_recv(3.6, exact=False), _drv("bidirectional"))
    assert r.status == "UNRESOLVABLE"
    assert "push-pull" in r.unresolvable_reason


# ── U8 wiring ───────────────────────────────────────────────────────────────

def test_u8_pin_match_in_core_and_reexported():
    assert "steps/pin_match.py" in provenance.CORE_PIPELINE_FILES
    assert step_04b_extractor.resolve_pin_spec_from_groups is pin_match.resolve_pin_spec_from_groups
    assert step_04b_extractor.pin_match_meta is pin_match.pin_match_meta


# ── U9 receiver-guard default stays untrusted ───────────────────────────────

def test_u9_receiver_guard_fires_on_dataclass_default_pin_type_exact():
    # No pin_type_exact kwarg at all: the ValidatedPinSpec dataclass default
    # (False, step_05_validator.py:91) must be treated exactly like an explicit
    # exact=False — the default is never implicitly trusted as a positive match.
    recv = ValidatedPinSpec(
        part_number="Rcv", pin_name="IN", pin_id="1",
        VIH_max=None, VIH_min=None, absolute_max_voltage=3.6, pin_type="input",
        source_snippet="abs max", signal_score=75, confidence="high",
        is_verified=True,
    )
    r = _sig(recv, _drv("output"))
    assert r.status == "UNRESOLVABLE"
    assert r.unresolvable_reason == (
        "Receiver U_RCV pin IN matched no pin group; "
        "default-group limits are not evidence for this pin")
