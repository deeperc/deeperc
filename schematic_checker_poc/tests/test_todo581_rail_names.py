"""TODO-581 / TODO-573: rail-name forms and the R-footprint value token.

Four narrow changes, ruled 2026-11-22 (TODO-581 card, decisions 1-3):
  * C1  step_08d _POWER_RAIL_RE reads a P<digits>V<digits> segment as a rail
        (P3V3, P1V8, P12V), so a pull-up to P3V3 is seen by step_08d / 08e / 08g.
  * V1  resistance_ohms skips an R<4 digits> footprint token inside an
        underscore-joined value string (R0402_4K7_1%_0.0625W_100PPM is 4.7k, not
        0.0402 ohm under the 10 ohm jumper floor). A bare "R0402" is unchanged.
  * C2a step_06 POWER_NET_RE accepts <digits>V<digits> with an optional REG or EXT
        suffix and no underscore (3V3, 5V, 5VREG, /12Vext); /5VPG and 3V3_EN stay
        signals.
  * C3b step_06 POWER_NET_RE accepts /?VDD<word> followed by -<word> groups
        (/VDD1A-2A, TODO-573). It is a rail with no voltage from its name.
  * steps/component_values.py is in CORE_PIPELINE_FILES (it moves verdicts).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import provenance
from steps.component_values import resistance_ohms
from steps.m2_output_conflict import _is_power_or_ground_net
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR
from steps.step_06_power import (
    POWER_NET_RE,
    infer_voltage_from_name,
    is_power_net_deterministic,
)
from steps.step_08d_peripheral_checker import _has_pullup, _is_power_rail_net
from steps.step_08e_pullup_value_checker import _pullup_sites
from steps.step_08g_pullup_presence import _reaches_power_rail


# ── C1: step_08d rail form P<digits>V<digits> ─────────────────────────────────

@pytest.mark.parametrize("name", ["P3V3", "P1V8", "P12V", "P1V2", "P0V85", "P3V3_CLK",
                                  "P5V_STDBY_VME", "/P3V3", "+P3V3"])
def test_c1_p_form_is_a_pullup_rail(name):
    assert _is_power_rail_net(name) is True


@pytest.mark.parametrize("name", ["P3", "PB5", "PGOOD", "/SFP0_SCL", "SPI_P3V3", "PV3"])
def test_c1_not_a_rail(name):
    assert _is_power_rail_net(name) is False


def test_c1_step_08g_walk_reads_p3v3():
    assert _reaches_power_rail("P3V3") is True
    assert _reaches_power_rail("P1V8") is True


# ── V1: R-footprint token inside an underscore-joined value ───────────────────

@pytest.mark.parametrize("s, expected", [
    ("R0402_4K7_1%_0.0625W_100PPM", 4_700.0),
    ("R0402_10K_1%_0.0625W_100PPM", 10_000.0),
    ("R0402_100R_1%_0.0625W_100PPM", 100.0),
    ("r0603_2k2_1%", 2_200.0),
    ("R0603_0R_1%", 0.0),
    ("R0402_1R0_1%_0.0625W_100PPM", 1.0),
])
def test_v1_footprint_token_skipped(s, expected):
    assert resistance_ohms(s) == pytest.approx(expected)


@pytest.mark.parametrize("s, expected", [
    ("R0402", 0.0402),       # bare token: not underscore-joined, unchanged
    ("R47", 0.47),
    ("R100", 0.1),
    ("R_2k2_0402", 2_200.0),
    ("2.2k/R0402", 2_200.0),
    ("4k7", 4_700.0),
])
def test_v1_other_forms_unchanged(s, expected):
    assert resistance_ohms(s) == pytest.approx(expected)


def test_v1_all_tokens_footprints_is_none():
    assert resistance_ohms("R0402_R0603") is None


def _pullup_netlist(value, rail):
    r = ComponentIR(refdes="R31", part_number="", value=value,
                    pins=[PinIR("1", "", "/SFP0_SCL"), PinIR("2", "", rail)])
    u = ComponentIR(refdes="U1", part_number="", value="SFP",
                    pins=[PinIR("5", "SCL", "/SFP0_SCL")])
    nets = [NetIR(name="/SFP0_SCL", pins=[("R31", "1"), ("U1", "5")]),
            NetIR(name=rail, pins=[("R31", "2")])]
    return NetlistIR(source_file="x", components=[r, u], nets=nets)


def test_c1_v1_vme_wren_shape_pullup_is_seen():
    ir = _pullup_netlist("R0402_4K7_1%_0.0625W_100PPM", "P3V3")
    assert _has_pullup(ir.nets[0], ir) is True
    sites = list(_pullup_sites(ir))
    assert len(sites) == 1


def test_c1_v1_zero_ohm_link_still_floored():
    ir = _pullup_netlist("R0402_0R_1%_0.0625W_100PPM", "P3V3")
    assert _has_pullup(ir.nets[0], ir) is False
    assert list(_pullup_sites(ir)) == []


# ── C2a: step_06 <digits>V<digits><word> ──────────────────────────────────────

@pytest.mark.parametrize("name, volts", [
    ("5VREG", 5.0), ("3V3", 3.3), ("5V", 5.0), ("/12Vext", 12.0), ("12V", 12.0),
])
def test_c2a_power_net_and_voltage(name, volts):
    assert is_power_net_deterministic(name) is True
    assert infer_voltage_from_name(name) == pytest.approx(volts)


@pytest.mark.parametrize("name", ["3V3_EN", "5V_RST", "P3V3_CLK", "/SFP0_SCL", "V3", "3VREG_EN",
                                  "/5VPG", "5VPG", "3V3EN", "5VOK"])
def test_c2a_not_a_power_net(name):
    assert is_power_net_deterministic(name) is False


def test_c2a_5vpg_stays_a_signal():
    # vme-wren power-supply-3 /5VPG (TPS62125 PG -> TPS74801 EN) is the project's
    # first real-hardware find; a <digits>V<word> rule must not turn it into a rail
    # (step_08 would skip the net). The suffix list is closed: REG and EXT only.
    assert POWER_NET_RE.match("/5VPG") is None


def test_c2a_output_conflict_skips_5vreg():
    assert _is_power_or_ground_net("5VREG", [], []) is True
    assert _is_power_or_ground_net("3V3_EN", [], []) is False


# ── C3b (TODO-573): hyphenated VDD rail ───────────────────────────────────────

@pytest.mark.parametrize("name", ["/VDD1A-2A", "VDD1A-2A", "VDDIO-A", "/VDD_A-B-C"])
def test_c3b_hyphen_vdd_is_power_net(name):
    assert is_power_net_deterministic(name) is True


def test_c3b_no_voltage_from_name():
    assert infer_voltage_from_name("/VDD1A-2A") is None


@pytest.mark.parametrize("name", ["/VCC_SENSE-ERROR*", "/PWR_3,3-5V", "Net-(U4-VDD1A)",
                                  "/VDD1A-", "VDD-", "/SD-CMD"])
def test_c3b_narrow(name):
    assert POWER_NET_RE.match(name) is None


# ── era hash ──────────────────────────────────────────────────────────────────

def test_component_values_is_an_era_member():
    assert "steps/component_values.py" in provenance.CORE_PIPELINE_FILES
    assert "steps/component_values.py" in provenance.CHECKER_SOURCE_FILES
