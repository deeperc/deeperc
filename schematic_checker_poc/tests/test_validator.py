import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps.step_05_validator import validate, validate_supply_group


def test_high_confidence():
    spec = validate(
        part_number="STM32F103C8T6",
        pin_name="PA0",
        pin_id="10",
        VIH_max=3.6,
        absolute_max_voltage=4.0,
        pin_type="bidirectional",
        source_snippet="VIH max 3.6V (5V tolerant pins excluded)",
    )
    assert spec.signal_score >= 60
    assert spec.confidence == "high"
    assert spec.is_verified is True


def test_low_confidence_all_nulls():
    spec = validate(
        part_number="STM32F103C8T6",
        pin_name="PA0",
        pin_id="10",
        VIH_max=None,
        absolute_max_voltage=None,
        pin_type=None,
        source_snippet=None,
    )
    assert spec.signal_score < 30
    assert spec.confidence == "low"
    assert spec.is_verified is False


def test_implausible_vih_no_range_bonus():
    spec_implausible = validate(
        part_number="X",
        pin_name="P1",
        pin_id="1",
        VIH_max=999.0,
        absolute_max_voltage=None,
        pin_type=None,
        source_snippet=None,
    )
    spec_plausible = validate(
        part_number="X",
        pin_name="P1",
        pin_id="1",
        VIH_max=3.6,
        absolute_max_voltage=None,
        pin_type=None,
        source_snippet=None,
    )
    # Plausible gets +25 (not null) + +15 (in range) = 40
    # Implausible gets +25 (not null) only = 25
    assert spec_plausible.signal_score > spec_implausible.signal_score
    assert spec_implausible.signal_score == 25


# ── Supply group validation tests ─────────────────────────────────────────────

def test_supply_range_passes():
    assert validate_supply_group({"supply_min": 3.0, "supply_max": 3.6}).ok is True


def test_supply_point_value_rejected():
    result = validate_supply_group({"supply_min": 3.3, "supply_max": 3.3})
    assert result.ok is False
    assert result.reason == "supply_min_equals_supply_max"
    assert result.evidence is not None


def test_supply_missing_max_passes():
    """Only supply_min present — different problem, this rule doesn't fire."""
    assert validate_supply_group({"supply_min": 3.3}).ok is True


def test_ft232rl_signature_rejected():
    """Exact FT232RL extraction cache signature is rejected (supply_min=supply_max=3.3, abs_max=5.5)."""
    result = validate_supply_group({"supply_min": 3.3, "supply_max": 3.3, "supply_abs_max": 5.5})
    assert result.ok is False
    assert result.reason == "supply_min_equals_supply_max"
    assert "3.3" in result.evidence
