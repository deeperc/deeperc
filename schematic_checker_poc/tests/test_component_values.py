"""Tests for the shared resistance_ohms value parser (component_values).

Covers every corpus value-string format from the M4-pullup recon Step 2 plus
the two mutant variant values (100, 100k) and genuinely-unparseable inputs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from steps.component_values import resistance_ohms


@pytest.mark.parametrize(
    "s, expected",
    [
        # plain numeric + unit
        ("10K", 10_000.0),
        ("10k", 10_000.0),
        ("5.1k", 5_100.0),
        ("4.7k", 4_700.0),
        ("2k", 2_000.0),
        ("1M", 1_000_000.0),
        ("470R", 470.0),
        ("470", 470.0),
        # the two mutant variant values (Nelson's spec)
        ("100", 100.0),      # 100Ω "too strong" -> should FAIL (<250)
        ("100k", 100_000.0),  # 100kΩ "too weak" -> should WARN (>10k, <500k)
        ("250", 250.0),       # FAIL band lower boundary
        ("500k", 500_000.0),  # FAIL band upper boundary
        ("1k", 1_000.0),      # WARN band lower boundary
        # RKM / IEC-60062 infix
        ("2k2", 2_200.0),
        ("1k5", 1_500.0),
        ("4k7", 4_700.0),
        ("10kR", 10_000.0),   # multiplier then trailing R noise
        ("4R7", 4.7),
        ("2M2", 2_200_000.0),
        ("R47", 0.47),        # leading-R sub-ohm
        # European comma decimal
        ("4,7K", 4_700.0),
        ("2,2K", 2_200.0),
        # symbol-name-embedded (the dominant corpus form)
        ("R_2k2_0402", 2_200.0),
        ("R_200k_0402", 200_000.0),
        ("R_4k7_0402", 4_700.0),
        ("R_10_0402", 10.0),
        # value/footprint
        ("2.2k/R0402", 2_200.0),
        # whitespace tolerance
        ("  10K  ", 10_000.0),
    ],
)
def test_resistance_ohms_parses(s, expected):
    assert resistance_ohms(s) == pytest.approx(expected)


@pytest.mark.parametrize(
    "s",
    [
        None,
        "",
        "   ",
        "DNP",
        "N/A",
        "do_not_populate",
        "abc",
    ],
)
def test_resistance_ohms_unparseable_returns_none(s):
    assert resistance_ohms(s) is None


def test_explicit_spec_cases():
    # asserts required verbatim by the build prompt
    assert resistance_ohms("R_200k_0402") == 200_000.0
    assert resistance_ohms("4,7K") == 4_700.0
    assert resistance_ohms("2k2") == 2_200.0
