import pytest
from steps.step_06_power import (
    is_power_net_deterministic,
    is_ground_net_deterministic,
    infer_voltage_from_name,
)


def test_bare_vcc_recognized_as_power():
    assert is_power_net_deterministic("VCC")
    assert is_power_net_deterministic("VDD")
    assert is_power_net_deterministic("VBAT")
    assert is_power_net_deterministic("VBUS")
    assert is_power_net_deterministic("VIN")


def test_bare_gnd_recognized_as_ground():
    assert is_ground_net_deterministic("GND")
    assert is_ground_net_deterministic("VSS")
    assert is_ground_net_deterministic("AGND")
    assert is_ground_net_deterministic("DGND")


def test_suffixed_power_rails_recognized():
    assert is_power_net_deterministic("VCC_3V3")
    assert is_power_net_deterministic("VDD_1V8")
    assert is_power_net_deterministic("+3V3")   # KiCad voltage-suffix format
    assert is_power_net_deterministic("+5V")


def test_bare_vcc_voltage_unknown():
    assert infer_voltage_from_name("VCC") is None
    assert infer_voltage_from_name("VDD") is None
    assert infer_voltage_from_name("GND") is None


def test_suffixed_voltage_resolves():
    assert infer_voltage_from_name("VCC_3V3") == 3.3
    assert infer_voltage_from_name("+5V") == 5.0
    assert infer_voltage_from_name("VDD_1V8") == 1.8


def test_signal_nets_not_recognized_as_power():
    assert not is_power_net_deterministic("MOSI")
    assert not is_power_net_deterministic("SDA")
    assert not is_power_net_deterministic("RESET")
    assert not is_power_net_deterministic("SCK")


# ── Fix γ: Tier-1 augmentation (survey §4 sketches #1–#4) ─────────────────────
from steps.step_06_power import classify_power_augmented, _GAMMA_VOLTAGE_RAIL_RE


def test_gamma_sketch1_slash_and_voltage_suffix():
    # Voltage-rail names that miss POWER_NET_RE's ^…$ anchoring.
    assert classify_power_augmented("/+5V_USB") == (True, 5.0)
    assert classify_power_augmented("+5V_EXT") == (True, 5.0)
    assert classify_power_augmented("+3.3VLAN") == (True, 3.3)
    assert classify_power_augmented("+3.3VA") == (True, 3.3)
    assert classify_power_augmented("P5V_VME") == (True, 5.0)
    assert classify_power_augmented("/+3.3V") == (True, 3.3)


def test_gamma_sketch1_negatives():
    # No voltage token → not a sketch-#1 match.
    assert classify_power_augmented("/MOTORA") is None
    # +VBAT has no numeric voltage: must NOT match sketch #1's voltage regex…
    assert _GAMMA_VOLTAGE_RAIL_RE.match("+VBAT") is None
    # …but IS caught by sketch #4 as an unknown-voltage rail.
    assert classify_power_augmented("+VBAT") == (True, None)


def test_gamma_sketch2_named_vcc():
    assert classify_power_augmented("AVCC") == (True, None)
    assert classify_power_augmented("XVCC") == (True, None)
    assert classify_power_augmented("USBVCC") == (True, None)
    assert classify_power_augmented("ISL_VCC") == (True, None)
    # Existing rules still own bare VCC / VCCIO (no regression, no double-claim).
    assert is_power_net_deterministic("VCC")
    assert is_power_net_deterministic("VCCIO")


def test_gamma_sketch3_synthetic_voltage_token():
    assert classify_power_augmented("Net-(BAT1-Pad1)") == (True, None)        # rail token, no voltage (real corpus case)
    assert classify_power_augmented("Net-(5.0V/3.3V1-Pad2)") == (True, None)  # multi-token → ambiguous (real corpus case)
    assert classify_power_augmented("Net-(5V0-Pad1)") == (True, 5.0)          # single voltage token
    assert classify_power_augmented("Net-(R1-Pad1)") is None                  # no rail/voltage token


def test_gamma_sketch4_batt_family():
    assert classify_power_augmented("+BATT") == (True, None)
    assert classify_power_augmented("BATT") == (True, None)
    # VBAT / VBATT remain owned by POWER_NET_RE (existing behavior).
    assert is_power_net_deterministic("VBAT")
    assert is_power_net_deterministic("VBATT")
    # BATCH-style signal names must NOT be mistaken for a battery rail.
    assert classify_power_augmented("BATCH") is None


# ── Fix γ: Tier-2 Gemma removal — would-be-ambiguous nets fall through ─────────
from steps.step_06_power import infer_power_nets
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR


def _ir_with_net(name, n_pins):
    comp = ComponentIR(refdes="U1", part_number="GENERIC", value="",
                       pins=[PinIR(pin_id=str(i), pin_name="P", net="") for i in range(n_pins)])
    return NetlistIR(source_file="/tmp/t.net", components=[comp],
                     nets=[NetIR(name=name, pins=[("U1", str(i)) for i in range(n_pins)])])


def test_gamma_highfanout_unclassified_net_falls_through_no_gemma():
    # A high-fanout, non-signal net Tier-1 can't name used to go to Gemma; now it
    # must simply stay unclassified — no rail, no ground, and (critically) no
    # network call / exception. No LLM is stubbed here, proving the call is gone.
    rails, grounds = infer_power_nets(_ir_with_net("/Boost Converter Vin", 5))
    assert all(r.net_name != "/Boost Converter Vin" for r in rails)
    assert "/Boost Converter Vin" not in grounds


def test_gamma_augmented_rail_classified_without_gemma():
    rails, _ = infer_power_nets(_ir_with_net("/+5V_USB", 5))
    hit = [r for r in rails if r.net_name == "/+5V_USB"]
    assert hit and hit[0].voltage_v == 5.0 and hit[0].source == "deterministic"


# ── TODO-409 Option 1-G: KiCad leading '/' on sheet-local ground labels ───────
# GROUND_NET_RE is anchored ^(?:...)$  with no slash tolerance, unlike the Fix-γ
# power-side augmentation layer (_GAMMA_VCC_RE etc., which is `^/?`-tolerant).
# The fix adds the same `^/?` to GROUND_NET_RE only — POWER_NET_RE is untouched.

def test_ground_net_leading_slash_g1_g2():
    # Slashed forms: accepted after the fix, rejected before it.
    assert is_ground_net_deterministic("/GND")
    assert is_ground_net_deterministic("/GNDA")
    assert is_ground_net_deterministic("/AGND")
    assert is_ground_net_deterministic("/DGND")
    # Unslashed forms: must remain accepted (no regression).
    assert is_ground_net_deterministic("GND")
    assert is_ground_net_deterministic("GNDA")
    assert is_ground_net_deterministic("AGND")
    assert is_ground_net_deterministic("DGND")
    # No widening beyond the slash: these stay rejected by GROUND_NET_RE.
    assert not is_ground_net_deterministic("/PWR_LED_K")
    assert not is_ground_net_deterministic("/Vref")
    assert not is_ground_net_deterministic("VBAT")
    assert not is_ground_net_deterministic("/v+")
    # Pre-existing looseness (independent of the slash fix, NOT altered by it):
    # GND\w* is greedy and un-suffix-anchored, so a pin-name-shaped string like
    # "GNDGPLL_G7" is accepted whole (matches GND + \w* through to the end) —
    # this was already true before the edit and remains true after.
    assert is_ground_net_deterministic("GNDGPLL_G7")


# ── TODO-409 Option 1-G: pipeline-level reproducer ────────────────────────────
# Real production chain (parse -> step_06 -> step_07 -> step_08c), the same
# call shape checker_registry.py wires for the "structural" CheckerSpec, run
# directly against a hand-built fixture rather than through full run_board
# (mirrors tests/test_todo303_wild_shaped_fixtures.py's "call the real
# production functions directly" pattern) — no resolver/network/PDF needed,
# since ground-net classification and structural checking are both pure
# netlist-topology + net-name operations.
import os as _os

from steps import step_07_confirm
from steps.rail_map import load_rail_map as _load_rail_map
from steps.step_02_parser import parse_netlist as _parse_netlist
from steps.step_08c_structural_checker import check_structural_integrity as _check_structural

_TODO409_FIXTURE = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)),
    "fixtures", "todo409_local_ground_labels.net")


def _run_structural_chain(fixture_path):
    ir = _parse_netlist(fixture_path)
    rail_map = _load_rail_map(fixture_path, None)
    power_rails, ground_nets = infer_power_nets(ir, rail_map=rail_map)
    power_rails_for_confirm = [
        {"net_name": r.net_name, "voltage_v": r.voltage_v,
         "confidence": r.confidence, "source": r.source}
        for r in power_rails
    ]
    confirmed_voltages = step_07_confirm.confirm_voltages(
        power_rails_for_confirm, skip=True)
    results = _check_structural(
        components=ir.components, confirmed_voltages=confirmed_voltages,
        ground_nets=ground_nets, nets=ir.nets, pintypes=ir.pintypes)
    return ground_nets, results


def test_todo409_slashed_ground_labels_no_false_fail_warn():
    ground_nets, results = _run_structural_chain(_TODO409_FIXTURE)

    # The ground-net set the pipeline exposes must include both slashed labels.
    assert "/GND" in ground_nets
    assert "/GNDA" in ground_nets

    # No structural finding of status FAIL or WARN may name either net — before
    # the fix this FAILS (VSSA/'/GNDA' single-pin-floating FAIL,
    # VSS/'/GND' unclassified-net WARN), since neither slashed label reaches
    # ground_nets and step_08c's branch-3 PASS never fires for them.
    bad = [r for r in results
           if r.status in ("FAIL", "WARN") and r.connected_net in ("/GND", "/GNDA")]
    assert bad == [], [(r.refdes, r.pin_name, r.connected_net, r.status,
                         r.evidence_label) for r in bad]
