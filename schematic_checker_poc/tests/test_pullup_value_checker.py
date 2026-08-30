"""Tests for step_08e I2C pull-up value-range checker (M4-pullup)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from steps.step_02_parser import ComponentIR, PinIR, NetIR, NetlistIR
from steps.step_08e_pullup_value_checker import (
    classify_ohms, check_pullup_values, PullupBand,
)


@pytest.mark.parametrize(
    "ohms, band",
    [
        (100.0, PullupBand.FAIL),      # 100Ω mutant "too strong"
        (249.0, PullupBand.FAIL),      # just below FAIL band
        (250.0, PullupBand.WARN),      # FAIL lower boundary inclusive -> WARN zone
        (500.0, PullupBand.WARN),
        (999.0, PullupBand.WARN),      # just below WARN band
        (1_000.0, PullupBand.PASS),    # WARN lower boundary inclusive
        (5_100.0, PullupBand.PASS),    # corpus mode
        (10_000.0, PullupBand.PASS),   # WARN upper boundary inclusive
        (10_001.0, PullupBand.WARN),   # just above WARN band
        (100_000.0, PullupBand.WARN),  # 100kΩ mutant "too weak"
        (500_000.0, PullupBand.WARN),  # FAIL upper boundary inclusive
        (500_001.0, PullupBand.FAIL),  # just above FAIL band
        (1_000_000.0, PullupBand.FAIL),
        (None, PullupBand.UNRESOLVABLE),
    ],
)
def test_classify_ohms_boundaries(ohms, band):
    sev, _desc = classify_ohms(ohms)
    assert sev is band


def _netlist_with_pullup(value):
    """Minimal IR: R1 pull-up on /SDA to +3V3, plus U1 the I2C master."""
    r1 = ComponentIR(
        refdes="R1", part_number="", value=value,
        pins=[PinIR("1", "", "/SDA"), PinIR("2", "", "+3V3")],
    )
    u1 = ComponentIR(
        refdes="U1", part_number="MCU", value="MCU",
        pins=[PinIR("5", "SDA", "/SDA")],
    )
    nets = [
        NetIR(name="/SDA", pins=[("R1", "1"), ("U1", "5")]),
        NetIR(name="+3V3", pins=[("R1", "2")]),
    ]
    return NetlistIR(source_file="x", components=[r1, u1], nets=nets)


def test_100ohm_mutant_fails():
    f = check_pullup_values(_netlist_with_pullup("100"))
    assert len(f) == 1
    assert f[0].severity is PullupBand.FAIL
    assert f[0].refdes == "R1"
    assert f[0].ohms == 100.0


def test_100k_mutant_warns():
    f = check_pullup_values(_netlist_with_pullup("100k"))
    assert len(f) == 1
    assert f[0].severity is PullupBand.WARN
    assert f[0].ohms == 100_000.0


def test_legit_pullup_is_silent():
    # 4.7k is a normal pull-up -> PASS -> no finding emitted
    assert check_pullup_values(_netlist_with_pullup("4.7k")) == []
    # corpus symbol-name form, in-band
    assert check_pullup_values(_netlist_with_pullup("R_2k2_0402")) == []


def test_unparseable_value_routes_unresolvable():
    f = check_pullup_values(_netlist_with_pullup("DNP"))
    assert len(f) == 1
    assert f[0].severity is PullupBand.UNRESOLVABLE
    assert f[0].ohms is None


def test_non_i2c_net_resistor_ignored():
    # R1 on /GPIO7 (not SDA/SCL) to +3V3 -> not a pull-up site
    r1 = ComponentIR(refdes="R1", part_number="", value="100",
                     pins=[PinIR("1", "", "/GPIO7"), PinIR("2", "", "+3V3")])
    nets = [NetIR(name="/GPIO7", pins=[("R1", "1")]),
            NetIR(name="+3V3", pins=[("R1", "2")])]
    assert check_pullup_values(NetlistIR(source_file="x", components=[r1], nets=nets)) == []


def test_resistor_not_to_rail_ignored():
    # R1 on /SDA but other pin on a signal net, not a rail -> not a pull-up
    r1 = ComponentIR(refdes="R1", part_number="", value="100",
                     pins=[PinIR("1", "", "/SDA"), PinIR("2", "", "/SOME_SIGNAL")])
    nets = [NetIR(name="/SDA", pins=[("R1", "1")]),
            NetIR(name="/SOME_SIGNAL", pins=[("R1", "2")])]
    assert check_pullup_values(NetlistIR(source_file="x", components=[r1], nets=nets)) == []
