"""Fork-6 (TODO-165) — pull-up-surface rail-vocabulary widen (Option C = A+B).

step_08d._is_power_rail_net (D) borrowed step_08c._is_rail_name's normalize +
per-segment split + a NARROWED token subset, so the pull-up surface (M3 NO_PULLUP
presence, M4 value via step_08e, de-dup) recognizes hierarchical/decorated rails
like csi's `/CSIA_I2C_VCC`. Deliberately narrower than C:
  * suffix branch is `_VCC$`/`_VDD$`-family ONLY — NOT `_PWR$`/`_VSW$` (porting
    `_PWR$` made a 0Ω link to `FLASH_PWR` a false pull-up: the Cycle-A STOP);
  * tokens add PVCC/PVDD/IOVCC/IOVDD/COREVDD/VBAT/VSYS only — NOT VIN/VOUT/VSW
    (switching nodes) nor VREF;
  * dotted-decimal `+3.3V` still matches (ESP32-PoE2 / kit-dev M4 sites);
  * Fix #1: bare `+EN`/`+RUN` no longer match.

Option B — jumper-class floor: a resistor < PULLUP_MIN_OHMS (0Ω link/shunt) is
not a pull-up. The floor MUST keep 100Ω sites (the M4 mutants) reaching step_08e.

C is asserted BYTE-UNCHANGED (its exact form underpins M7's single-pin-float
immunity), including its known dotted-decimal gap.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from steps.step_02_parser import ComponentIR, PinIR, NetIR, NetlistIR
from steps.step_08d_peripheral_checker import (
    _is_power_rail_net as D_is_rail,
    _has_pullup,
    PULLUP_MIN_OHMS,
)
from steps.step_08c_structural_checker import _is_rail_name as C_is_rail
from steps.step_08e_pullup_value_checker import _pullup_sites


# Rails D gained (narrowed subset) + the hierarchical rail that motivated Cycle A.
D_NEW_MATCHES = [
    "PVCC", "PVDD", "IOVCC", "IOVDD", "COREVDD", "VBAT", "VSYS",
    "/CSIA_I2C_VCC", "CSIA_I2C_VCC", "/CSIB_I2C_VCC",
]

# D-preserve set — must STILL match (landmine guard: dotted-decimal +3.3V).
D_PRESERVE_MATCHES = [
    "+3V3", "+5V", "+3.3V", "+1.8V", "3V3", "5V",
    "VCC", "VDD", "VBUS", "VCCIO", "AVDD", "DVDD", "PWR",
    "/+3V3_AUX",
]

# Narrowing exclusions + Fix #1 + signals — must NOT match.
# FLASH_PWR (the Cycle-A STOP), VIN/VOUT/VSW/VREF (deliberately dropped tokens).
D_NON_MATCHES = [
    "FLASH_PWR", "VIN", "VOUT", "VSW", "VREF",
    "+EN", "+RUN", "+HELLO", "EN", "/SCL", "/SDA",
    "GND", "/I2C_MUX_SDA", "RESET", "/MISO", "/MOSI",
]


@pytest.mark.parametrize("name", D_NEW_MATCHES)
def test_D_gained_narrowed_tokens_and_hierarchical_rails(name):
    assert D_is_rail(name) is True, f"D should recognize rail {name!r}"


@pytest.mark.parametrize("name", D_PRESERVE_MATCHES)
def test_D_preserves_plus_voltage_forms(name):
    assert D_is_rail(name) is True, f"D must still recognize {name!r} (regression)"


@pytest.mark.parametrize("name", D_NON_MATCHES)
def test_D_narrowing_and_fix1_exclusions(name):
    assert D_is_rail(name) is False, f"D must NOT recognize {name!r}"


def test_D_does_not_match_FLASH_PWR_the_cycleA_STOP():
    # The precise regression that STOPPED Cycle A: _PWR$ suffix must be absent.
    assert D_is_rail("FLASH_PWR") is False
    assert D_is_rail("SOME_VSW") is False   # _VSW$ suffix also dropped


# ── C-regression guard: C (step_08c) is byte-unchanged by Fork-6 ──────────────

@pytest.mark.parametrize("net,expected", [
    ("VCC", True), ("VDD", True), ("VSYS", True), ("IOVDD", True),
    ("/CSIA_I2C_VCC", True), ("+3V3", True), ("+5V", True),
    # C DOES still match the tokens D dropped (C is unchanged, only D narrowed):
    ("VIN", True), ("VOUT", True), ("VSW", True), ("VREF", True),
    ("FLASH_PWR", True),   # C's _PWR$ suffix is intact (safe there — M7 PASS-only)
    # … and C rejects signals / control nets:
    ("+EN", False), ("+RUN", False), ("/SCL", False), ("GND", False),
])
def test_C_behavior_pinned(net, expected):
    assert C_is_rail(net) is expected, f"C behavior changed for {net!r}"


def test_C_dotted_decimal_gap_is_preserved():
    # C's KNOWN gap (separate carded item): C does NOT match dotted-decimal +3.3V.
    assert C_is_rail("+3.3V") is False
    # D, by contrast, DOES — the reason the widen lives D-side.
    assert D_is_rail("+3.3V") is True


def test_D_is_deliberately_narrower_than_C():
    # The Cycle-A STOP fix: D drops what C keeps on the pull-up surface.
    for name in ["FLASH_PWR", "VIN", "VOUT", "VSW", "VREF"]:
        assert C_is_rail(name) is True and D_is_rail(name) is False, (
            f"{name!r}: expected C=True (unchanged) but D=False (narrowed)")


# ── Option B — jumper-class floor ─────────────────────────────────────────────

def _i2c_pullup_netlist(resistor_value: str):
    """Minimal netlist: a 2-pin resistor bridging an I2C SDA net to +3V3."""
    r = ComponentIR(refdes="R1", part_number="", value=resistor_value,
                    pins=[PinIR("1", "", "/SDA"), PinIR("2", "", "+3V3")])
    u = ComponentIR(refdes="U1", part_number="", value="MCU",
                    pins=[PinIR("5", "SDA", "/SDA")])
    nets = [NetIR(name="/SDA", pins=[("R1", "1"), ("U1", "5")]),
            NetIR(name="+3V3", pins=[("R1", "2")])]
    return NetlistIR(source_file="x", components=[r, u], nets=nets)


def test_floor_excludes_zero_ohm_link():
    # 0Ω link to a rail is NOT a pull-up site.
    sites = list(_pullup_sites(_i2c_pullup_netlist("R_0R_0402")))
    assert sites == [], "0Ω link must be floored out of pull-up-site detection"
    assert _has_pullup(_i2c_pullup_netlist("R_0R_0402").nets[0],
                       _i2c_pullup_netlist("R_0R_0402")) is False


def test_floor_keeps_100_ohm_site_reaching_step_08e():
    # LANDMINE GUARD: the M4 mutants are 100Ω. 100 > PULLUP_MIN_OHMS, so a 100Ω
    # site MUST still be detected — flooring it out would silently collapse M4.
    assert PULLUP_MIN_OHMS <= 100.0
    sites = list(_pullup_sites(_i2c_pullup_netlist("R_100_0402")))
    assert len(sites) == 1, "100Ω pull-up site must reach step_08e (M4 recall)"


def test_floor_keeps_unparseable_value_as_candidate():
    # An unparseable value is never silently dropped — it reaches step_08e as
    # UNRESOLVABLE, not floored out.
    sites = list(_pullup_sites(_i2c_pullup_netlist("DNP")))
    assert len(sites) == 1, "unparseable value must remain a pull-up site"


def test_floor_keeps_normal_pullup():
    sites = list(_pullup_sites(_i2c_pullup_netlist("4k7")))
    assert len(sites) == 1
