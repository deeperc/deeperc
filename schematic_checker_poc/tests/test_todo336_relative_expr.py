"""TODO-336: rail-relative threshold expressions (<field>_expr / <field>_alts),
evaluated in step_05 against the receiver part's own rail pin voltage and
surfaced by step_08 in evidence_label / unresolvable_reason."""
import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import extraction_input, step_04b_extractor  # noqa: E402
from steps import step_05_validator as V  # noqa: E402
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR  # noqa: E402
from steps.step_05_validator import ValidatedPinSpec, parse_rail_expr, validate  # noqa: E402
from steps.step_08_checker import run  # noqa: E402


# ── parser ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,want", [
    ("0.7*VDD", (0.7, 0.0, "VDD")),
    ("VDD*0.7", (0.7, 0.0, "VDD")),
    ("0.7 VDD", (0.7, 0.0, "VDD")),
    ("0.7×VCC", (0.7, 0.0, "VCC")),
    ("0.7xVCC", (0.7, 0.0, "VCC")),
    ("0.7 x VCC", (0.7, 0.0, "VCC")),
    ("VCC x 0.7", (0.7, 0.0, "VCC")),
    ("VCC+0.3", (1.0, 0.3, "VCC")),
    ("VCC + 0.3V", (1.0, 0.3, "VCC")),
    ("VCC-0.3 V", (1.0, -0.3, "VCC")),
    ("VCC−0.4", (1.0, -0.4, "VCC")),
    ("(VDD)-1.5", (1.0, -1.5, "VDD")),
    ("(V+)-1.5V", (1.0, -1.5, "V+")),
    ("V+ - 1.5", (1.0, -1.5, "V+")),
    ("0.3 × VDD_IO", (0.3, 0.0, "VDD_IO")),
])
def test_parser_accepts(text, want):
    k, c, sym = parse_rail_expr(text)
    assert (round(k, 9), round(c, 9), sym) == want


def test_parser_scaled_inner_offset():
    k, c, sym = parse_rail_expr("0.7*(VCC-0.5)+0.1")
    assert sym == "VCC" and k == pytest.approx(0.7) and c == pytest.approx(-0.25)


@pytest.mark.parametrize("text", [
    "VDD*VCC", "0.7*VDD + 0.1*VCC",       # two symbols
    "0.7", "0.7 V", "2.31",                 # no symbol
    "BVCEO", "(V+)", "supply_abs_max",      # bare symbol (no number)
    "garbage!", "", "   ", "2*VDD/3",       # garbage / unsupported operator
    "VDD-VDD",                              # zero rail coefficient
    "0.7*(VCC-0.5", None, 3.3,              # unbalanced / not a string
])
def test_parser_rejects(text):
    assert parse_rail_expr(text) is None


# ── rail resolution ──────────────────────────────────────────────────────────
def test_rail_match_normalizes_names():
    assert V.resolve_rail_voltage("VDD_IO", [("vdd-io", 1.8), ("GND", None)]) == (1.8, None)
    assert V.resolve_rail_voltage("V+", [("V+", 12.0), ("V-", -12.0)]) == (12.0, None)


def test_rail_no_match():
    assert V.resolve_rail_voltage("VDD", [("VCC", 3.3)]) == (None, V.RAIL_SYMBOL_NOT_ON_PART)
    assert V.resolve_rail_voltage("VDD", None) == (None, V.RAIL_SYMBOL_NOT_ON_PART)


def test_rail_all_none():
    assert V.resolve_rail_voltage("VCC", [("VCC", None), ("VCC", None)]) == (
        None, V.RAIL_VOLTAGE_UNKNOWN)


def test_rail_two_distinct_voltages():
    assert V.resolve_rail_voltage("VCC", [("VCC", 3.3), ("VCC", 5.0)]) == (None, V.RAIL_AMBIGUOUS)
    # the same voltage on several matching pins is not ambiguous
    assert V.resolve_rail_voltage("VCC", [("VCC", 3.3), ("VCC", 3.3), ("VCC", None)]) == (3.3, None)


# ── TODO-509 fork (d): rail-name normalisation, digit-led symbols, footnote fallback ──
def test_leading_plus_stripped_once_trailing_plus_kept():
    assert V._norm_rail_name("+VA") == "VA" and V._norm_rail_name("+3V3") == "3V3"
    assert V._norm_rail_name("V+") == "V+" and V._norm_rail_name("++V") == "+V"
    assert V.resolve_rail_voltage("V+", [("V+", 12.0), ("V", 5.0)]) == (12.0, None)


def test_plus_va_round_trips_against_board_rail():
    assert parse_rail_expr("+VA + 0.3") == (1.0, 0.3, "VA")
    assert V.resolve_field("absolute_max_voltage", None, "+VA + 0.3", rails=[("+VA", 5.0)])[0] == 5.3


@pytest.mark.parametrize("text,want", [
    ("+3V3*0.7", (0.7, 0.0, "3V3")),
    ("0.7*1V8", (0.7, 0.0, "1V8")),
    ("3V3-0.4", (1.0, -0.4, "3V3")),
    ("0.7VDD", (0.7, 0.0, "VDD")),           # no digit after the letters: still num * sym
    ("0.7VDD1", (0.7, 0.0, "VDD1")),         # "." before the digit run: still num * sym
])
def test_digit_led_symbol_is_one_token(text, want):
    k, c, sym = parse_rail_expr(text)
    assert (round(k, 9), round(c, 9), sym) == want


def test_3v3_expression_resolves_against_plus_3v3():
    v, note = V.resolve_field("VIH_min", None, "+3V3*0.7", rails=[("+3V3", 3.3)])
    assert v == pytest.approx(2.31) and "3V3=3.3" in note


def test_footnote_digit_fallback():
    v, note = V.resolve_field("VIL_max", None, "0.75×VDD1", rails=[("VDD", 3.3)])
    assert v == pytest.approx(2.475) and "footnote fallback VDD1→VDD" in note
    v, note = V.resolve_field("VIL_max", None, "0.75×VDD1", rails=[("VDD", 3.3), ("VDD1", 1.8)])
    assert v == pytest.approx(1.35) and "footnote" not in note
    notes = []
    assert V.resolve_rail_voltage("VCC1", [("VDD", 3.3)], notes) == (None, V.RAIL_SYMBOL_NOT_ON_PART)
    assert notes == []


# ── validate(): precedence, reasons, legacy identity ─────────────────────────
def _v(**kw):
    base = dict(part_number="P", pin_name="IN", pin_id="1", VIH_max=None,
                absolute_max_voltage=None, pin_type="digital_input",
                source_snippet="DC characteristics 5.0")
    base.update(kw)
    return validate(**base)


def test_expr_evaluated_against_rail():
    s = _v(VIH_min_expr="0.7*VDD", absolute_max_voltage_expr="VDD+0.3",
           rails=[("VDD", 5.0), ("GND", None)])
    assert s.VIH_min == 3.5 and s.absolute_max_voltage == 5.3
    assert s.expr_notes == ["VIH_min = 0.7*VDD @ VDD=5.0 V → 3.5 V",
                            "absolute_max_voltage = VDD+0.3 @ VDD=5.0 V → 5.3 V"]


def test_expr_wins_over_numeric():
    s = _v(VIH_min=2.31, VIH_min_expr="0.7 VDD", rails=[("VDD", 5.0)])
    assert s.VIH_min == 3.5
    assert s.expr_notes == ["VIH_min = 0.7 VDD @ VDD=5.0 V → 3.5 V [numeric 2.31 discarded]"]
    # unresolvable expr: the flattened numeric is still discarded, not used as fallback
    s = _v(VIH_min=2.31, VIH_min_expr="0.7 VDD", rails=[("VCC", 3.3)])
    assert s.VIH_min is None
    assert s.expr_notes == [
        "VIH_min unevaluated: RAIL_SYMBOL_NOT_ON_PART (VDD) [numeric 2.31 discarded]"]


def test_alts_are_conditional_alternatives():
    alts = [{"condition": "VCC=2V", "value": 1.5, "expr": None},
            {"condition": "VCC=4.5V", "value": 3.15, "expr": None}]
    s = _v(VIH_min=3.15, VIH_min_alts=alts, rails=[("VCC", 4.5)])
    assert s.VIH_min is None
    assert s.expr_notes == [
        "VIH_min unevaluated: CONDITIONAL_ALTERNATIVES (2 rows); numeric 3.15 discarded"]


def test_non_numeric_string():
    s = _v(VIH_min="BVCEO", absolute_max_voltage="VCES")
    assert s.VIH_min is None and s.absolute_max_voltage is None
    assert s.expr_notes == ["VIH_min unevaluated: NON_NUMERIC_VALUE ('BVCEO')",
                            "absolute_max_voltage unevaluated: NON_NUMERIC_VALUE ('VCES')"]


def test_bare_symbol_only_from_explicit_expr():
    assert parse_rail_expr("VCC", allow_bare=True) == (1.0, 0.0, "VCC")
    s = _v(VIH_min_expr="VCC", absolute_max_voltage_expr="(V+)", rails=[("VCC", 3.3), ("V+", 5.0)])
    assert (s.VIH_min, s.absolute_max_voltage) == (3.3, 5.0)
    assert s.expr_notes == ["VIH_min = VCC @ VCC=3.3 V → 3.3 V",
                            "absolute_max_voltage = (V+) @ V+=5.0 V → 5.0 V"]
    # a legacy in-field string keeps the >=1-number rule
    s = _v(VIH_min="VCC", absolute_max_voltage="BVCEO", rails=[("VCC", 3.3)])
    assert (s.VIH_min, s.absolute_max_voltage) == (None, None)
    assert s.expr_notes == ["VIH_min unevaluated: NON_NUMERIC_VALUE ('VCC')",
                            "absolute_max_voltage unevaluated: NON_NUMERIC_VALUE ('BVCEO')"]


def test_legacy_in_field_expression():
    s = _v(VIH_min="0.7 × VCC", rails=[("VCC", 3.3)])
    assert s.VIH_min == 2.31
    assert s.expr_notes == ["VIH_min = 0.7 × VCC @ VCC=3.3 V → 2.31 V [legacy in-field expression]"]


def test_unparseable_and_rail_unknown_reasons():
    s = _v(VIH_min_expr="0.7*VDD/2", absolute_max_voltage_expr="VCC+0.3",
           rails=[("VCC", None)])
    assert s.expr_notes == ["VIH_min unevaluated: EXPR_UNPARSEABLE ('0.7*VDD/2')",
                            "absolute_max_voltage unevaluated: RAIL_VOLTAGE_UNKNOWN (VCC)"]


def _pre336(VIH_min, absolute_max_voltage):
    """The pre-TODO-336 validate() scoring, verbatim, for the legacy identity check."""
    score = 0
    vih_val = VIH_min
    if vih_val is not None:
        score += 25
        if isinstance(vih_val, (int, float)) and 0.5 <= vih_val <= 20.0:
            score += 15
    if absolute_max_voltage is not None:
        score += 20
        if isinstance(absolute_max_voltage, (int, float)) and 1.0 <= absolute_max_voltage <= 50.0:
            score += 10
    score += 15 + 10 + 5          # the fixed snippet (len>10, has a digit) and pin_type
    if vih_val is not None and absolute_max_voltage is not None and abs(vih_val - absolute_max_voltage) < 0.01:
        score -= 20
    conf = "high" if score >= 60 else "medium" if score >= 30 else "low"
    return ValidatedPinSpec(part_number="P", pin_name="IN", pin_id="1", VIH_max=None,
                            VIH_min=VIH_min, absolute_max_voltage=absolute_max_voltage,
                            pin_type="digital_input", source_snippet="DC characteristics 5.0",
                            signal_score=score, confidence=conf, is_verified=(conf != "low"))


@pytest.mark.parametrize("vih,amax", [(2.0, 5.5), (None, 5.5), (2.0, None), (None, None),
                                      (3.3, 3.3), (0.2, 60.0)])
def test_legacy_group_identical(vih, amax):
    got = _v(VIH_min=vih, absolute_max_voltage=amax)
    assert dataclasses.asdict(got) == dataclasses.asdict(_pre336(vih, amax))
    assert got.expr_notes == []


def test_legacy_numeric_strings_unchanged():
    # what pipeline's removed _to_float pre-conversion used to produce
    got = _v(VIH_min="2.0", absolute_max_voltage="5.5")
    assert (got.VIH_min, got.absolute_max_voltage, got.expr_notes) == (2.0, 5.5, [])


def test_validate_pin_groups_stays_numeric_only():
    out = V.validate_pin_groups("P", {"pin_groups": [
        {"group_name": "g", "VIH_min": None, "VIH_min_expr": "0.7*VDD", "absolute_max_voltage": "x"}]})
    assert out["g"].VIH_min is None and out["g"].absolute_max_voltage is None
    assert out["g"].expr_notes == []


# ── prompts ──────────────────────────────────────────────────────────────────
def test_prompts_format_and_carry_the_rule():
    t = extraction_input.EXTRACTION_PROMPT_TEMPLATE.format(
        part_number="P", source_md="s.md", output_path="o.json", markdown_content="m")
    assert "<field>_expr" in t and "<field>_alts" in t and "Never" in t
    assert '"VIH_min_expr"' not in t.split("Schema:")[1]      # not in the schema example
    p = step_04b_extractor.PART_EXTRACTION_PROMPT.format(part_number="P", section_text="x")
    assert "<field>_expr" in p and "compute it using" not in p
    e = step_04b_extractor.EXTRACTION_PROMPT.format(part_number="P", pin_name="IN", pin_id="1",
                                                    section_text="x")
    assert "<field>_expr" in e and "compute the value" not in e


# ── step_08 end to end ───────────────────────────────────────────────────────
def _board(rcv_vcc_v, rcv_rail_pin="VCC"):
    ir = NetlistIR(
        source_file="t",
        components=[
            ComponentIR(refdes="U_DRV", part_number="D", value="D", pins=[
                PinIR(pin_id="1", pin_name="OUT", net="SIG"),
                PinIR(pin_id="2", pin_name="VCC", net="VCC_3V3")]),
            ComponentIR(refdes="U_RCV", part_number="R", value="R", pins=[
                PinIR(pin_id="1", pin_name="IN", net="SIG"),
                PinIR(pin_id="2", pin_name=rcv_rail_pin, net="VCC_RCV")]),
        ],
        nets=[NetIR(name="SIG", pins=[("U_DRV", "1"), ("U_RCV", "1")]),
              NetIR(name="VCC_3V3", pins=[("U_DRV", "2")]),
              NetIR(name="VCC_RCV", pins=[("U_RCV", "2")])],
        power_nets=["VCC_3V3", "VCC_RCV"], ground_nets=[])
    confirmed = {"VCC_3V3": 3.3, "VCC_RCV": rcv_vcc_v}
    return ir, confirmed


def _run(recv_spec, ir, confirmed):
    drv = ValidatedPinSpec(part_number="D", pin_name="OUT", pin_id="1", VIH_max=None, VIH_min=None,
                           absolute_max_voltage=None, pin_type="output", source_snippet=None,
                           signal_score=5, confidence="low", is_verified=False,
                           pin_type_exact=True)
    res = run(ir, {("U_DRV", "OUT"): drv, ("U_RCV", "IN"): recv_spec}, confirmed)
    return next(r for r in res if r.net_name == "SIG")


def _recv_spec(ir, confirmed, **kw):
    comp = next(c for c in ir.components if c.refdes == "U_RCV")
    s = validate(part_number="R", pin_name="IN", pin_id="1", VIH_max=None,
                 absolute_max_voltage=None, pin_type="digital_input",
                 source_snippet="DC characteristics table 1",
                 rails=[(p.pin_name, confirmed.get(p.net)) for p in comp.pins], **kw)
    s.pin_type_exact = True
    return s


def test_step08_evaluated_vih_drives_warn():
    ir, confirmed = _board(5.0)
    r = _run(_recv_spec(ir, confirmed, VIH_min_expr="0.7*VCC"), ir, confirmed)
    assert r.status == "WARN" and r.receiver_VIH_min == 3.5
    assert r.evidence_label == (
        "driver 3.3V is below receiver VIH_min 3.5V — signal may not register as logic HIGH"
        " [VIH_min = 0.7*VCC @ VCC=5.0 V → 3.5 V]")
    assert r.unresolvable_reason is None


def test_step08_unresolved_expr_names_reason():
    ir, confirmed = _board(5.0, rcv_rail_pin="AVCC")
    r = _run(_recv_spec(ir, confirmed, VIH_min_expr="0.7*VDD"), ir, confirmed)
    assert r.status == "UNRESOLVABLE"
    assert r.evidence_label == (
        "Assumed/low-confidence [VIH_min unevaluated: RAIL_SYMBOL_NOT_ON_PART (VDD)]")
    assert r.unresolvable_reason == (
        "Receiver specs unresolved — U_RCV: VIH_min unevaluated: RAIL_SYMBOL_NOT_ON_PART (VDD)")


def test_step08_legacy_spec_text_unchanged():
    ir, confirmed = _board(5.0)
    r = _run(_recv_spec(ir, confirmed, VIH_min=3.5), ir, confirmed)
    assert r.status == "WARN"
    assert r.evidence_label == (
        "driver 3.3V is below receiver VIH_min 3.5V — signal may not register as logic HIGH")
