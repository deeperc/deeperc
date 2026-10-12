"""TODO-531 N2u — the NAME-RELATED number bind of a named pin (steps/pin_match.py).

Amends TODO-488 J2 only for callers that pass part_pins: a named pin whose S1-S3
stages find nothing may bind by number ("number_name") when the entry at its
number is prefix- or suffix-related to its name and names no other pin of the
part. Without part_pins every function behaves as before (test_todo488_pairs.py
and test_todo504_pin_match.py pin that, unedited). Synthetic fixtures only.
"""

import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps.pin_match import (  # noqa: E402
    listing_supply_groups, match_groups, pin_match_meta, resolve_pin_spec_from_groups,
)
from steps.step_02_parser import ComponentIR, PinIR  # noqa: E402


def _g(name, ex, pt="digital_input", **kw):
    d = {"group_name": name, "pin_type": pt, "example_pins": ex}
    d.update(kw)
    return d


def _names(hits):
    return [h["group_name"] for h in hits]


# ── the binds the checkpoint measured RIGHT ─────────────────────────────────

AMS_VIN = _g("Input", ["3|VIN"], "power", supply_max=12.0)
AMS_VOUT = _g("Output", ["2|VOUT", "4|VOUT"], "power")
AMS_ADJ = _g("Adjust / ground", ["1|ADJ/GND"], "power")
AMS = [AMS_ADJ, AMS_VOUT, AMS_VIN]
AMS_PINS = [("1", "ADJ/GND", ""), ("2", "VO", ""), ("3", "VI", "")]

TPS_EN = _g("Enable", ["5|ENA"])
TPS = [_g("Boot", ["1|BOOT"]), _g("Sense", ["4|VSENSE"]), TPS_EN, _g("Phase", ["8|PH"], "digital_output")]
TPS_PINS = [("1", "BOOT", ""), ("2", "NC", ""), ("3", "NC", ""), ("4", "VSENSE", ""), ("5", "EN", ""),
            ("6", "GND", ""), ("7", "VIN", ""), ("8", "PH", "")]

ESP_IO = _g("GPIO", ["10|GPIO25", "11|GPIO26"], "bidirectional")
ESP = [_g("Power", ["2|3V3"], "power", supply_max=3.6), ESP_IO]
ESP_PINS = [("2", "3V3", ""), ("10", "IO25", ""), ("11", "IO26", "")]


def test_ams1117_vin_entry_binds_board_vi():
    assert match_groups("3", "VI", AMS, part_pins=AMS_PINS) == ("number_name", [AMS_VIN])


def test_ams1117_vout_entry_binds_board_vo():
    assert match_groups("2", "VO", AMS, part_pins=AMS_PINS) == ("number_name", [AMS_VOUT])


def test_tps5430_ena_entry_binds_board_en():
    assert match_groups("5", "EN", TPS, part_pins=TPS_PINS) == ("number_name", [TPS_EN])


def test_suffix_related_gpio25_entry_binds_board_io25():
    assert match_groups("10", "IO25", ESP, part_pins=ESP_PINS) == ("number_name", [ESP_IO])


def test_symbol_name_counts_for_relatedness():
    # board name is the pin number; the libparts symbol name VI is what relates to VIN
    pins = [("1", "1", "ADJ/GND"), ("2", "2", "VO"), ("3", "3", "VI")]
    assert match_groups("3", "3", AMS, symbol_name="VI", part_pins=pins) == ("number_name", [AMS_VIN])


# ── the refusals ─────────────────────────────────────────────────────────────

LP_BP = _g("Bypass", ["4|BYPASS"], "analog")
LP = [_g("Input", ["1|VIN"], "power", supply_max=16.0), LP_BP, _g("Output", ["5|VOUT"], "power")]
LP_PINS = [("1", "VIN", ""), ("2", "GND", ""), ("3", "ON/~{OFF}", ""), ("4", "BP", ""), ("5", "VOUT", "")]

VCC_G = _g("Supply", ["61|VCC"], "power", supply_max=5.5)
VCC_GROUPS = [VCC_G, _g("IO", ["10|PA0"], "bidirectional")]
VCC_PINS = [("60", "VCC", ""), ("61", "VCC2", ""), ("10", "PA0", "")]

RP_IO = _g("GPIO", ["26|GPIO2", "4|GPIO3"], "bidirectional")
RP = [RP_IO]
RP_PINS = [("4", "GPIO2", ""), ("26", "GPIO25", "")]

D_A = _g("Anode", ["1|Anode"], "analog")
D_K = _g("Cathode", ["2|Cathode"], "analog")
DIODE = [D_A, D_K]
DIODE_PINS = [("1", "K", ""), ("2", "A", "")]

TWO = [_g("Input", ["3|VIN"], "power", supply_max=12.0), _g("Sense", ["3|VIN_SENSE"], "analog")]


def test_lp2985_bypass_entry_does_not_bind_board_bp():
    assert match_groups("4", "BP", LP, part_pins=LP_PINS) == ("num_conflict", [])


def test_vcc_entry_does_not_bind_vcc2_when_another_pin_is_vcc():
    assert match_groups("61", "VCC2", VCC_GROUPS, part_pins=VCC_PINS) == ("num_conflict", [])


def test_vcc_entry_binds_vcc2_when_no_other_pin_is_vcc():
    pins = [("61", "VCC2", ""), ("10", "PA0", "")]
    assert match_groups("61", "VCC2", VCC_GROUPS, part_pins=pins) == ("number_name", [VCC_G])


def test_rp2040_gpio2_entry_does_not_bind_gpio25_when_a_gpio2_pin_exists():
    assert match_groups("26", "GPIO25", RP, part_pins=RP_PINS) == ("num_conflict", [])


def test_uniqueness_reads_the_other_pins_symbol_name():
    pins = [("4", "4", "GPIO2"), ("26", "GPIO25", "")]
    assert match_groups("26", "GPIO25", RP, part_pins=pins) == ("num_conflict", [])


def test_uniqueness_reads_the_other_pins_alternates():
    # S3 key: the other pin's "GPIO2/SDA" splits on "/" to GPIO2
    pins = [("4", "GPIO2/SDA", ""), ("26", "GPIO25", "")]
    assert match_groups("26", "GPIO25", RP, part_pins=pins) == ("num_conflict", [])


def test_the_pin_itself_in_part_pins_does_not_block():
    pins = [("26", "GPIO25", "GPIO2"), ("4", "GPIO3", "")]
    assert match_groups("26", "GPIO25", RP, part_pins=pins) == ("number_name", [RP_IO])


def test_1n4148_anode_entry_does_not_bind_board_k():
    assert match_groups("1", "K", DIODE, part_pins=DIODE_PINS) == ("num_conflict", [])


def test_single_character_names_are_never_related():
    gs = [_g("Drain", ["3|D2"], "analog")]
    assert match_groups("3", "D", gs, part_pins=[("3", "D", "")]) == ("num_conflict", [])


def test_pair_at_the_number_in_two_groups_does_not_bind():
    assert match_groups("3", "VI", TWO, part_pins=AMS_PINS) == ("num_conflict", [])


CASES = [("3", "VI", "", AMS, AMS_PINS), ("2", "VO", "", AMS, AMS_PINS), ("5", "EN", "", TPS, TPS_PINS),
         ("10", "IO25", "", ESP, ESP_PINS), ("4", "BP", "", LP, LP_PINS),
         ("61", "VCC2", "", VCC_GROUPS, VCC_PINS), ("26", "GPIO25", "", RP, RP_PINS),
         ("1", "K", "", DIODE, DIODE_PINS), ("3", "VI", "", TWO, AMS_PINS)]


@pytest.mark.parametrize("pid,pname,sym,groups,pins", CASES)
def test_without_part_pins_every_case_is_num_conflict(pid, pname, sym, groups, pins):
    res = {"pin_groups": groups, "default_group": None}
    assert match_groups(pid, pname, groups, symbol_name=sym) == ("num_conflict", [])
    assert match_groups(pid, pname, groups, symbol_name=sym, part_pins=None) == ("num_conflict", [])
    assert pin_match_meta(pid, pname, res)[0] is False
    assert listing_supply_groups(pid, pname, res) == []


def test_nameless_pin_still_binds_by_number():
    assert match_groups("3", "3", AMS) == ("number", [AMS_VIN])
    assert match_groups("3", "3", AMS, part_pins=AMS_PINS) == ("number", [AMS_VIN])
    assert match_groups("3", "", AMS, part_pins=AMS_PINS) == ("number", [AMS_VIN])


def test_name_stages_still_win_over_the_number_bind():
    gs = [_g("Input", ["3|VIN"], "power", supply_max=12.0), _g("Other", ["7|VI"], "analog")]
    assert match_groups("3", "VI", gs, part_pins=AMS_PINS) == ("exact", [gs[1]])


# ── resolve / meta / listing on a "number_name" hit ─────────────────────────

def test_resolve_and_meta_on_a_number_name_hit():
    res = {"pin_groups": TPS, "default_group": "Phase"}
    assert resolve_pin_spec_from_groups("5", "EN", res, part_pins=TPS_PINS) is TPS_EN
    assert resolve_pin_spec_from_groups("5", "EN", res)["group_name"] == "Phase"   # J2: fallback
    assert pin_match_meta("5", "EN", res, part_pins=TPS_PINS) == (True, False)
    assert pin_match_meta("5", "EN", res) == (False, False)


def test_listing_supply_groups_admits_a_number_name_hit_on_a_ceiling_power_group():
    res = {"pin_groups": AMS}
    assert listing_supply_groups("3", "VI", res, part_pins=AMS_PINS) == [AMS_VIN]
    assert listing_supply_groups("3", "VI", res) == []


def test_listing_supply_groups_refuses_a_number_name_hit_without_a_ceiling():
    res = {"pin_groups": AMS}
    assert match_groups("2", "VO", AMS, part_pins=AMS_PINS)[0] == "number_name"
    assert listing_supply_groups("2", "VO", res, part_pins=AMS_PINS) == []         # VOUT: no ceiling


def test_listing_supply_groups_refuses_a_number_name_hit_on_a_non_power_group():
    res = {"pin_groups": TPS}
    assert listing_supply_groups("5", "EN", res, part_pins=TPS_PINS) == []


def test_step08b_wrapper_threads_the_components_pins():
    from steps import step_08b_supply_checker as s8b
    pins = [PinIR(pin_id=p, pin_name=n, net=f"N{p}") for p, n, _s in AMS_PINS]
    comp = ComponentIR(refdes="U1", part_number="AMS1117-3.3", value="AMS1117-3.3", pins=pins)
    res = {"pin_groups": AMS}
    assert s8b._listing_supply_groups(pins[2], res, comp) == [AMS_VIN]
    assert s8b._listing_supply_groups(pins[2], res) == []                          # no comp: J2


# ── part_pins=None changes nothing on the TODO-488 legacy parity table ──────

def _todo488():
    path = os.path.join(os.path.dirname(__file__), "test_todo488_pairs.py")
    spec = importlib.util.spec_from_file_location("_todo488_pairs_fixture", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_legacy_parity_table_unchanged_with_and_without_part_pins():
    m = _todo488()
    res = {"pin_groups": m.LEGACY_GROUPS, "default_group": "SIG2"}
    every = [(c[0], c[1], "") for c in m.LEGACY_CASES]
    for pid, pname, kind, hits, resolved, meta, listing in m.LEGACY_CASES:
        for pp in (None, every):
            k, h = match_groups(pid, pname, m.LEGACY_GROUPS, part_pins=pp)
            assert (k, _names(h)) == (kind, hits)
            assert resolve_pin_spec_from_groups(pid, pname, res, part_pins=pp)["group_name"] == resolved
            assert list(pin_match_meta(pid, pname, res, part_pins=pp)) == meta
            assert _names(listing_supply_groups(pid, pname, res, part_pins=pp)) == listing
