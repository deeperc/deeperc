"""Unit tests for passive_traversal helper functions."""
import sys
import os
import pytest
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.passive_traversal import (
    parse_resistor_value,
    classify_component,
    confidence_for_bridge,
    Confidence,
    BridgeType,
    ComponentRef,
    _REJECT,
)


@dataclass
class _Comp:
    refdes: str
    value: str


# ── parse_resistor_value ──────────────────────────────────────────────────────

class TestParseResistorValue:
    def test_zero(self):
        assert parse_resistor_value("0") == 0.0

    def test_zero_r(self):
        assert parse_resistor_value("0R") == 0.0

    def test_zero_omega(self):
        assert parse_resistor_value("0Ω") == 0.0

    def test_eia_0r0(self):
        assert parse_resistor_value("0R0") == 0.0

    def test_eia_1r5(self):
        assert parse_resistor_value("1R5") == pytest.approx(1.5)

    def test_integer(self):
        assert parse_resistor_value("10") == 10.0

    def test_10r(self):
        assert parse_resistor_value("10R") == 10.0

    def test_100r(self):
        assert parse_resistor_value("100R") == 100.0

    def test_4k7_style(self):
        assert parse_resistor_value("4k7") == pytest.approx(4700.0)

    def test_4K7_uppercase(self):
        assert parse_resistor_value("4K7") == pytest.approx(4700.0)

    def test_10k5(self):
        assert parse_resistor_value("10k5") == pytest.approx(10500.0)

    def test_4_7k(self):
        assert parse_resistor_value("4.7k") == pytest.approx(4700.0)

    def test_1_5k(self):
        assert parse_resistor_value("1.5k") == pytest.approx(1500.0)

    def test_10k(self):
        assert parse_resistor_value("10k") == pytest.approx(10000.0)

    def test_1M(self):
        assert parse_resistor_value("1M") == pytest.approx(1_000_000.0)

    def test_empty_returns_none(self):
        assert parse_resistor_value("") is None

    def test_none_input_returns_none(self):
        assert parse_resistor_value(None) is None

    def test_complex_string_returns_none(self):
        assert parse_resistor_value("FB0805/600R/2A") is None

    def test_non_numeric_returns_none(self):
        assert parse_resistor_value("unknown") is None


# ── classify_component ────────────────────────────────────────────────────────

class TestClassifyComponent:
    def test_inductor(self):
        comp = _Comp("L1", "100uH")
        ref = classify_component(comp)
        assert ref is not None
        assert ref.bridge_type == BridgeType.INDUCTOR
        assert ref.refdes == "L1"

    def test_ferrite_bead_fb_prefix(self):
        comp = _Comp("FB1", "BLM18AG102SN1D")
        ref = classify_component(comp)
        assert ref is not None
        assert ref.bridge_type == BridgeType.INDUCTOR

    def test_l_prefixed_ferrite_bead(self):
        # Real KiCad: ferrite bead with refdes L, value FB...
        comp = _Comp("L2", "FB0805/600R/2A")
        ref = classify_component(comp)
        assert ref is not None
        assert ref.bridge_type == BridgeType.INDUCTOR

    def test_diode(self):
        comp = _Comp("D1", "1N4148")
        ref = classify_component(comp)
        assert ref is not None
        assert ref.bridge_type == BridgeType.DIODE

    def test_zero_ohm_jumper(self):
        comp = _Comp("R1", "0R")
        ref = classify_component(comp)
        assert ref is not None
        assert ref.bridge_type == BridgeType.JUMPER

    def test_zero_ohm_bare(self):
        comp = _Comp("R2", "0")
        ref = classify_component(comp)
        assert ref is not None
        assert ref.bridge_type == BridgeType.JUMPER

    def test_resistor(self):
        comp = _Comp("R3", "10k")
        ref = classify_component(comp)
        assert ref is not None
        assert ref.bridge_type == BridgeType.RESISTOR

    def test_capacitor_rejected(self):
        assert classify_component(_Comp("C1", "100n")) is None

    def test_ic_rejected(self):
        assert classify_component(_Comp("U1", "STM32F103")) is None

    def test_connector_rejected(self):
        assert classify_component(_Comp("J1", "USB-C")) is None

    def test_transistor_rejected(self):
        assert classify_component(_Comp("Q1", "2N3904")) is None

    def test_fuse_rejected(self):
        assert classify_component(_Comp("F1", "500mA")) is None

    def test_pseudo_component_rejected(self):
        assert classify_component(_Comp("#FLG01", "")) is None


# ── confidence_for_bridge ─────────────────────────────────────────────────────

class TestConfidenceForBridge:
    def test_jumper_high(self):
        ref = ComponentRef("R1", BridgeType.JUMPER, "0R")
        conf, reason = confidence_for_bridge(ref)
        assert conf == Confidence.HIGH
        assert "lossless" in reason

    def test_inductor_high(self):
        ref = ComponentRef("L1", BridgeType.INDUCTOR, "100uH")
        conf, reason = confidence_for_bridge(ref)
        assert conf == Confidence.HIGH
        assert "inductor" in reason

    def test_diode_medium(self):
        ref = ComponentRef("D1", BridgeType.DIODE, "1N4148")
        conf, reason = confidence_for_bridge(ref)
        assert conf == Confidence.MEDIUM

    def test_small_resistor_high(self):
        ref = ComponentRef("R1", BridgeType.RESISTOR, "10R")
        conf, reason = confidence_for_bridge(ref)
        assert conf == Confidence.HIGH

    def test_exactly_99ohm_high(self):
        ref = ComponentRef("R1", BridgeType.RESISTOR, "99R")
        conf, reason = confidence_for_bridge(ref)
        assert conf == Confidence.HIGH

    def test_100ohm_low(self):
        ref = ComponentRef("R1", BridgeType.RESISTOR, "100R")
        conf, reason = confidence_for_bridge(ref)
        assert conf == Confidence.LOW

    def test_1kohm_low(self):
        ref = ComponentRef("R1", BridgeType.RESISTOR, "1k")
        conf, reason = confidence_for_bridge(ref)
        assert conf == Confidence.LOW

    def test_large_resistor_reject(self):
        ref = ComponentRef("R1", BridgeType.RESISTOR, "10k")
        conf, reason = confidence_for_bridge(ref)
        assert conf is _REJECT

    def test_unparseable_resistor_medium(self):
        ref = ComponentRef("R1", BridgeType.RESISTOR, "unknown_value")
        conf, reason = confidence_for_bridge(ref)
        assert conf == Confidence.MEDIUM
