import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps.step_08b_supply_checker import (
    _find_matching_supply_group,
    _is_supply_pin,
    check_component_supplies,
    evaluate_supply,
    is_negative_rail,
    SUPPLY_PIN_NAMES,
)
from steps.step_02_parser import ComponentIR, PinIR


# ── voltage-aware supply-group selection (Notion 3933272c-88f3-81e8) ──────────
# Two groups share a rail name (SLB9673 dual-mode VDD): the pick must be by voltage
# containment, ORDER-INDEPENDENT — not first-in-list.

def _dual_vdd(order_high_first=True):
    hi = {"pin_type": "power", "supply_rail_name": "VDD", "supply_min": 3.0,
          "supply_max": 3.6, "supply_abs_max": 4.1}
    lo = {"pin_type": "power", "supply_rail_name": "VDD", "supply_min": 1.65,
          "supply_max": 1.95, "supply_abs_max": 4.1}
    groups = [hi, lo] if order_high_first else [lo, hi]
    return {"pin_groups": groups}


def test_voltage_pick_contains_high_group_order_high_first():
    g = _find_matching_supply_group("VDD", _dual_vdd(True), 3.3)
    assert g["supply_max"] == 3.6


def test_voltage_pick_contains_high_group_order_low_first():
    # the whole point: 3.3V still selects the 3.0-3.6 group even when 1.65-1.95 is listed FIRST
    g = _find_matching_supply_group("VDD", _dual_vdd(False), 3.3)
    assert g["supply_max"] == 3.6


def test_voltage_pick_low_mode_selects_low_group():
    # a board running the dual-mode part at 1.8V selects the 1.65-1.95 group, both orders
    assert _find_matching_supply_group("VDD", _dual_vdd(True), 1.8)["supply_max"] == 1.95
    assert _find_matching_supply_group("VDD", _dual_vdd(False), 1.8)["supply_max"] == 1.95


def test_voltage_pick_none_contains_uses_nearest_and_still_fails():
    # SLB9673 +12V: contained by neither; nearest is the 3.0-3.6 group → downstream FAIL preserved
    g = _find_matching_supply_group("VDD", _dual_vdd(True), 12.0)
    assert g["supply_max"] == 3.6  # nearest, not demoted to UNRESOLVABLE


def test_voltage_null_preserves_first_match():
    # V unknown → old first-match behaviour (list order)
    assert _find_matching_supply_group("VDD", _dual_vdd(True), None)["supply_max"] == 3.6
    assert _find_matching_supply_group("VDD", _dual_vdd(False), None)["supply_max"] == 1.95


def test_single_group_fast_path():
    pg = {"pin_groups": [{"pin_type": "power", "supply_rail_name": "VCC",
                          "supply_min": 3.0, "supply_max": 3.6}]}
    assert _find_matching_supply_group("VCC", pg, 3.3)["supply_max"] == 3.6


# ── negative-rail (bipolar) routing ───────────────────────────────────────────

def test_is_negative_rail_by_name_and_by_max():
    assert is_negative_rail({"supply_rail_name": "VCC-", "supply_max": 0.0})
    assert is_negative_rail({"supply_rail_name": "V-", "supply_max": None})
    assert is_negative_rail({"supply_rail_name": "VEE", "supply_max": 5.0})
    assert is_negative_rail({"supply_rail_name": "SOMENEG", "supply_max": 0.0})   # max<=0
    assert not is_negative_rail({"supply_rail_name": "VCC+", "supply_max": 40.0})


def test_positive_pin_fallback_excludes_negative_group():
    # TL072 V+ pin (no exact rail match): fallback must skip VCC- and land on VCC+
    pg = {"pin_groups": [
        {"pin_type": "power", "supply_rail_name": "VCC-", "supply_min": -40.0, "supply_max": 0.0},
        {"pin_type": "power", "supply_rail_name": "VCC+", "supply_min": 4.5, "supply_max": 40.0,
         "supply_abs_max": 36.0}]}
    g = _find_matching_supply_group("V+", pg, 12.0)
    assert g["supply_rail_name"] == "VCC+" and g["supply_max"] == 40.0


def test_negative_rail_pin_routes_unresolvable():
    # a pin whose OWN rail is negative → UNRESOLVABLE, never a max=0.0 false FAIL
    comp = ComponentIR("U1", "TL072CD", "", [PinIR("4", "VCC-", "-12V")])
    pg = {"TL072CD": {"pin_groups": [
        {"pin_type": "power", "supply_rail_name": "VCC-", "supply_min": -40.0, "supply_max": 0.0}]}}
    results = check_component_supplies([comp], pg, {"-12V": -12.0})
    assert len(results) == 1 and results[0].status == "UNRESOLVABLE"
    assert "Negative supply rail" in results[0].evidence_label


# ---------------------------------------------------------------------------
# _is_supply_pin — reduce-to-canonical widen (TODO-121)
#
# Invariant: a pin matches only if, after stripping a recognized voltage/bank/
# side decoration and normalizing separators, the residual stem is a member of
# the canonical SUPPLY_PIN_NAMES set. The widen can NEVER invent a new supply
# prefix.
# ---------------------------------------------------------------------------

import pytest


@pytest.mark.parametrize("name", [
    "VCC33", "VDD33", "VCCO_44", "VCCO_43", "VCC_A", "VCC_B",
    "VIN_3V3", "VDD_1V8", "VCC5V0", "VDDIO_1", "VDDA33", "VBAT2",
])
def test_is_supply_pin_decorated_positive(name):
    """Decorated forms that reduce to a canonical stem must match."""
    assert _is_supply_pin(name) is True


@pytest.mark.parametrize("name", sorted(SUPPLY_PIN_NAMES))
def test_is_supply_pin_canonical_regression(name):
    """Every existing canonical name still matches (incl. V+, VCC1, VCC2)."""
    assert _is_supply_pin(name) is True
    # case/whitespace insensitive
    assert _is_supply_pin(f"  {name.lower()}  ") is True


@pytest.mark.parametrize("name", [
    "VCC_EN", "VCCEN", "VCC_SEL", "VCCO_SENSE", "PG_VDD", "VDD_OK",
    "VDD_FB", "VC", "VCONTROL", "VCC_C", "SDA", "GND", "GPIO0", "RESET",
])
def test_is_supply_pin_negative(name):
    """Control/sense/status/signal pins and non-canonical residuals must NOT match."""
    assert _is_supply_pin(name) is False


@pytest.mark.parametrize("name", ["", None])
def test_is_supply_pin_empty(name):
    assert _is_supply_pin(name) is False


# ---------------------------------------------------------------------------
# Rail-name / matched-group mismatch guard (multi-rail part, single group)
# ---------------------------------------------------------------------------

from steps.step_08b_supply_checker import (
    _pin_name_nominal_voltage,
    _rail_name_group_mismatch,
)


@pytest.mark.parametrize("name,expected", [
    ("VDD5", 5.0), ("VCC33", 3.3), ("VDD33", 3.3), ("VIN_3V3", 3.3),
    ("VDD_1V8", 1.8), ("VCC5V0", 5.0), ("VCC50", 5.0), ("VCC18", 1.8),
    ("VCC3", 3.0), ("VCC1", 1.0), ("VCC2", 2.0),
    ("VCC", None), ("VDD", None), ("VCC_A", None), ("AVCC", None),
])
def test_pin_name_nominal_voltage(name, expected):
    assert _pin_name_nominal_voltage(name) == expected


def test_rail_name_mismatch_fires_for_5v_pin_on_3v3_group():
    """VDD5 (5V-named) matched to a 3.3V group (max 3.6) -> mismatch."""
    assert _rail_name_group_mismatch("VDD5", rated_max=3.6, rated_abs_max=4.6) == 5.0


def test_rail_name_mismatch_inert_for_witness_vcc33():
    """M1 witness: VCC33 (3.3V) on its own 3.3V group -> no mismatch -> FAIL kept."""
    assert _rail_name_group_mismatch("VCC33", rated_max=3.6, rated_abs_max=4.6) is None


def test_rail_name_mismatch_inert_for_index_digits():
    """Benign index digits (VCC1/VCC2) never exceed a real supply max."""
    assert _rail_name_group_mismatch("VCC1", rated_max=3.6, rated_abs_max=4.6) is None
    assert _rail_name_group_mismatch("VCC2", rated_max=3.6, rated_abs_max=4.6) is None
    assert _rail_name_group_mismatch("VCC", rated_max=3.6, rated_abs_max=4.6) is None


def test_vdd5_on_5v_net_downgrades_fail_to_unresolvable():
    """End-to-end: HD3SS3220-style multi-rail FP. VDD5 on +5V vs the only
    extracted group (3.3V VCC33, abs_max 4.6) must NOT FAIL — it's the wrong
    rail. Surfaces as UNRESOLVABLE."""
    comp = ComponentIR("U56", "HD3SS3220IRNHR", "", [
        PinIR("8", "VCC33", "+3V3"),
        PinIR("30", "VDD5", "+5V"),
    ])
    spec = {"pin_groups": [{
        "pin_type": "power", "supply_rail_name": "VCC33",
        "supply_min": 2.0, "supply_max": 3.6, "supply_abs_max": 4.6,
    }]}
    results = check_component_supplies(
        [comp], {"HD3SS3220IRNHR": spec}, {"+3V3": 3.3, "+5V": 5.0})
    by_pin = {r.supply_pin_name: r for r in results}
    assert by_pin["VCC33"].status == "PASS"          # 3.3 in range
    assert by_pin["VDD5"].status == "UNRESOLVABLE"   # FP suppressed
    assert "rail-name mismatch" in by_pin["VDD5"].evidence_label


def test_vcc33_on_5v_net_still_fails_witness():
    """The M1 witness path: VCC33 (3.3V-named) wired to +5V is a real
    over-voltage against its own 3.3V group — must remain FAIL."""
    comp = ComponentIR("U56", "HD3SS3220IRNHR", "", [
        PinIR("8", "VCC33", "+5V"),
    ])
    spec = {"pin_groups": [{
        "pin_type": "power", "supply_rail_name": "VCC33",
        "supply_min": 2.0, "supply_max": 3.6, "supply_abs_max": 4.6,
    }]}
    results = check_component_supplies(
        [comp], {"HD3SS3220IRNHR": spec}, {"+5V": 5.0})
    r = next(x for x in results if x.supply_pin_name == "VCC33")
    assert r.status == "FAIL"


def test_is_supply_pin_reduction_residual_must_be_canonical():
    """The reduce step can't turn a non-supply into a canonical member.
    VCC_C -> VCCC (not canonical) -> no match; arbitrary trailing letters are
    never stripped."""
    assert _is_supply_pin("VCC_C") is False     # VCCC not canonical
    assert _is_supply_pin("VCCC") is False
    assert _is_supply_pin("VCCEN") is False      # would need to strip "EN"
    # but a real decorated canonical still reduces:
    assert _is_supply_pin("VCC_A") is True       # VCCA canonical


def test_list_valued_supply_rail_name_does_not_crash():
    """LLM extractor can return supply_rail_name as a list; must not raise."""
    pin_groups_result = {
        "pin_groups": [
            {
                "pin_type": "power",
                "supply_rail_name": ["3V3"],
                "supply_min": 3.0,
                "supply_max": 3.6,
            }
        ]
    }
    result = _find_matching_supply_group("VCC", pin_groups_result)
    assert result is not None  # fallback group returned


def test_list_valued_rail_falls_back_to_spec_group():
    """When supply_rail_name is a list, fallback returns the group by spec presence."""
    pin_groups_result = {
        "pin_groups": [
            {
                "pin_type": "power",
                "supply_rail_name": ["3V3"],
                "supply_min": 3.0,
                "supply_max": 3.6,
            }
        ]
    }
    result = _find_matching_supply_group("VCC", pin_groups_result)
    assert result is not None
    assert result["supply_min"] == 3.0
    assert result["supply_max"] == 3.6


def test_dict_valued_supply_rail_name_does_not_crash():
    """supply_rail_name as a dict also must not crash."""
    pin_groups_result = {
        "pin_groups": [
            {
                "pin_type": "power",
                "supply_rail_name": {"name": "VCC"},
                "supply_min": 3.0,
                "supply_max": 3.6,
            }
        ]
    }
    result = _find_matching_supply_group("VCC", pin_groups_result)
    assert result is not None


def test_none_supply_rail_name_returns_fallback():
    """supply_rail_name: None falls through to fallback group."""
    pin_groups_result = {
        "pin_groups": [
            {
                "pin_type": "power",
                "supply_rail_name": None,
                "supply_min": 4.5,
                "supply_max": 5.5,
            }
        ]
    }
    result = _find_matching_supply_group("VCC", pin_groups_result)
    assert result is not None
    assert result["supply_min"] == 4.5


def test_exact_string_match_takes_priority():
    """Exact string match returned before fallback group."""
    pin_groups_result = {
        "pin_groups": [
            {
                "pin_type": "power",
                "supply_rail_name": "VCC",
                "supply_min": 4.5,
                "supply_max": 5.5,
            },
            {
                "pin_type": "power",
                "supply_rail_name": "VCCIO",
                "supply_min": 1.8,
                "supply_max": 3.3,
            },
        ]
    }
    result = _find_matching_supply_group("VCC", pin_groups_result)
    assert result is not None
    assert result["supply_min"] == 4.5


# ── Validator integration tests ───────────────────────────────────────────────

def _make_comp(pin_name: str, net: str) -> ComponentIR:
    return ComponentIR(
        refdes="U1",
        part_number="FT232RL",
        value="FT232RL",
        pins=[PinIR(pin_id="20", pin_name=pin_name, net=net)],
    )


def test_point_value_spec_produces_unresolvable():
    """Group with supply_min == supply_max rejected by validator → UNRESOLVABLE, not FAIL."""
    comp = _make_comp("VCC", "+5V")
    pin_groups = {
        "FT232RL": {
            "pin_groups": [
                {
                    "pin_type": "power",
                    "supply_rail_name": "VCC",
                    "supply_min": 3.3,
                    "supply_max": 3.3,
                    "supply_abs_max": 5.5,
                }
            ]
        }
    }
    confirmed = {"+5V": 5.0}
    results = check_component_supplies([comp], pin_groups, confirmed)
    assert len(results) == 1
    r = results[0]
    assert r.status == "UNRESOLVABLE"
    assert "supply_min_equals_supply_max" in r.evidence_label


def test_point_value_not_fail():
    """Validator rejection produces UNRESOLVABLE even when actual_v > point-spec max."""
    comp = _make_comp("VCC", "+5V")
    pin_groups = {
        "FT232RL": {
            "pin_groups": [
                {
                    "pin_type": "power",
                    "supply_rail_name": "VCC",
                    "supply_min": 3.3,
                    "supply_max": 3.3,
                }
            ]
        }
    }
    confirmed = {"+5V": 5.0}
    results = check_component_supplies([comp], pin_groups, confirmed)
    assert results[0].status != "FAIL"


# ── Option A: reject topologically-implausible converter VIN voltages ─────────

def _boost_spec(min_v=3.0, max_v=3.6, abs_max=4.6):
    return {"pin_groups": [{
        "pin_type": "power", "supply_rail_name": "VIN",
        "supply_min": min_v, "supply_max": max_v, "supply_abs_max": abs_max,
    }]}


def test_boost_vin_overvoltage_downgraded_to_unresolvable():
    """flight-computer U8: TPS61230 boost, /Boost Converter Vin classified 5.0V
    (> 4.6 abs-max) → would FAIL, but is downgraded to UNRESOLVABLE."""
    comp = ComponentIR("U8", "TPS61230DRC", "", [
        PinIR("10", "VIN", "/Boost Converter Vin"),
        PinIR("3", "VOUT", "+5V"),
    ])
    results = check_component_supplies(
        [comp], {"TPS61230DRC": _boost_spec()},
        {"/Boost Converter Vin": 5.0, "+5V": 5.0},
    )
    r = next(x for x in results if x.supply_pin_name == "VIN")
    assert r.status == "UNRESOLVABLE"
    assert "converter-VIN rejected" in r.evidence_label


def test_voltage_named_rail_not_second_guessed():
    """A converter VIN on a deterministically voltage-named rail (P12V) is
    trustworthy and must still FAIL if out of spec — never downgraded."""
    comp = ComponentIR("U8", "TPS61230DRC", "", [
        PinIR("10", "VIN", "P12V"),
    ])
    results = check_component_supplies(
        [comp], {"TPS61230DRC": _boost_spec()},
        {"P12V": 12.0},
    )
    r = next(x for x in results if x.supply_pin_name == "VIN")
    assert r.status == "FAIL"          # 12.0 > 4.6, and name is voltage-bearing


def test_in_window_converter_vin_unchanged():
    """LM3670 buck VIN at 5.0V is within [2.5,5.5] → PASS, untouched."""
    comp = ComponentIR("U9", "LM3670MF", "", [PinIR("1", "VIN", "/Buck Vin")])
    spec = {"pin_groups": [{
        "pin_type": "power", "supply_rail_name": "VIN",
        "supply_min": 2.5, "supply_max": 5.5, "supply_abs_max": 6.0,
    }]}
    results = check_component_supplies([comp], {"LM3670MF": spec}, {"/Buck Vin": 5.0})
    r = next(x for x in results if x.supply_pin_name == "VIN")
    assert r.status == "PASS"


def test_non_converter_overvoltage_still_fails():
    """A non-converter part (not in CONVERTER_FAMILY) keeps its real FAIL."""
    comp = ComponentIR("U1", "STM32F103C8T6", "", [PinIR("1", "VDD", "/Some_Rail")])
    spec = {"pin_groups": [{
        "pin_type": "power", "supply_rail_name": "VDD",
        "supply_min": 2.0, "supply_max": 3.6, "supply_abs_max": 4.0,
    }]}
    results = check_component_supplies([comp], {"STM32F103C8T6": spec}, {"/Some_Rail": 5.0})
    r = next(x for x in results if x.supply_pin_name == "VDD")
    assert r.status == "FAIL"


def test_boost_vin_at_or_above_vout_rejected_within_loose_window():
    """Boost-specific guard: a VIN that sits *inside* a loose rated window but is
    ≥ a known VOUT rail is topologically impossible for a boost → UNRESOLVABLE,
    even though evaluate_supply alone would PASS it."""
    comp = ComponentIR("U8", "TPS61230DRC", "", [
        PinIR("10", "VIN", "/Boost Vin"),
        PinIR("3", "VOUT", "+5V"),
    ])
    spec = {"pin_groups": [{   # window wide enough that 5.0 would PASS
        "pin_type": "power", "supply_rail_name": "VIN",
        "supply_min": 3.0, "supply_max": 6.0, "supply_abs_max": 7.0,
    }]}
    results = check_component_supplies(
        [comp], {"TPS61230DRC": spec}, {"/Boost Vin": 5.0, "+5V": 5.0})
    r = next(x for x in results if x.supply_pin_name == "VIN")
    assert r.status == "UNRESOLVABLE"
    assert "≥ VOUT" in r.evidence_label


# ── D2-shaped evidence labels (TODO-398) ────────────────────────────────────
# Every non-UNRESOLVABLE label is now "Confirmed — " prefixed, so
# step_10_report's existing startswith("Confirmed") tier-rewrite (frozen,
# _rewrite_for_tier) fires the same way it already does for every other
# checker's "Confirmed —" labels. abs_max_source is appended parenthetically
# only on the two abs-max-derived labels, only when supplied.

def test_unresolvable_label_unchanged_no_tier_prefix():
    status, label = evaluate_supply(None, 3.0, 3.6, 4.1)
    assert status == "UNRESOLVABLE"
    assert label == "Net voltage not confirmed"


def test_fail_abs_max_label_with_source():
    status, label = evaluate_supply(5.0, 3.0, 3.6, 4.1, abs_max_source="Table 6: Voltage characteristics")
    assert status == "FAIL"
    assert label == (
        "Confirmed — actual 5.0V exceeds supply absolute max 4.1V — "
        "device damage (Table 6: Voltage characteristics)"
    )


def test_fail_abs_max_label_without_source_omits_parenthetical():
    status, label = evaluate_supply(5.0, 3.0, 3.6, 4.1)
    assert status == "FAIL"
    assert label == "Confirmed — actual 5.0V exceeds supply absolute max 4.1V — device damage"
    assert "(None)" not in label
    assert "(" not in label


def test_warn_abs_max_label_with_source():
    status, label = evaluate_supply(4.0, 3.0, 3.6, 4.1, abs_max_source="Absolute Maximum Ratings")
    assert status == "WARN"
    assert label == (
        "Confirmed — actual 4.0V is within 5% of supply abs max 4.1V — "
        "transient overshoot risk (Absolute Maximum Ratings)"
    )


def test_fail_rated_max_label():
    status, label = evaluate_supply(3.8, 3.0, 3.6, None)
    assert status == "FAIL"
    assert label == "Confirmed — actual 3.8V exceeds rated supply max 3.6V — out of spec, likely damage"


def test_warn_rated_min_label():
    status, label = evaluate_supply(2.5, 3.0, 3.6, None)
    assert status == "WARN"
    assert label == "Confirmed — actual 2.5V is below rated supply min 3.0V — device may not operate correctly"


def test_pass_label():
    status, label = evaluate_supply(3.3, 3.0, 3.6, 4.1)
    assert status == "PASS"
    assert label == "Confirmed — supply 3.3V within rated range 3.0V - 3.6V"


def test_abs_max_source_threaded_from_pin_group_through_check_component_supplies():
    comp = ComponentIR("U1", "AMS1117-3.3", "", [PinIR("1", "VCC", "+5V")])
    spec = {"pin_groups": [{
        "pin_type": "power", "supply_rail_name": "VCC",
        "supply_min": 4.75, "supply_max": 5.25, "supply_abs_max": 6.0,
        "abs_max_source": "Table 6: Voltage characteristics",
    }]}
    results = check_component_supplies([comp], {"AMS1117-3.3": spec}, {"+5V": 7.0})
    r = results[0]
    assert r.status == "FAIL"
    assert "(Table 6: Voltage characteristics)" in r.evidence_label


def test_pass_label_round_trips_through_tier_rewrite_tier2_and_tier3():
    """P5: the new D2 labels' 'Confirmed — ' prefix must still be recognized
    by step_10_report's frozen _rewrite_for_tier — swapped to 'Cache-sourced'
    for tier-2/tier-3, with the frozen parentheticals appended, exactly like
    every other checker's existing 'Confirmed —' labels."""
    from steps import step_10_report as r10
    status, label = evaluate_supply(3.3, 3.0, 3.6, 4.1)
    assert label == "Confirmed — supply 3.3V within rated range 3.0V - 3.6V"

    tier2 = r10._rewrite_for_tier(label, r10.TIER_CACHE_UNVERIFIED)
    assert tier2 == "Cache-sourced — supply 3.3V within rated range 3.0V - 3.6V (not locally verified)"

    tier3 = r10._rewrite_for_tier(label, r10.TIER_CACHE_DRIFT)
    assert tier3 == (
        "Cache-sourced — supply 3.3V within rated range 3.0V - 3.6V "
        "(local datasheet differs from cache source)"
    )
