"""LT-21: the --board mode FINDINGS section (run_checks._render_findings).

Presentation-only, hermetic (a synthetic report dict, no pipeline run) — same
convention as test_corpus_result_dict.py. Locks:
  - FAILs render before WARNs, both before the UNRESOLVABLE section.
  - PASS entries are never itemized individually — only counted (TODO-417
    half 1: a trailing "(<N> PASS finding(s) not shown...)" note; omitted at
    N == 0).
  - peripheral findings print the evidence string VERBATIM, plus a
    'KB sources: ...' line only when kb_provenance is non-empty.
  - UNRESOLVABLE entries render FAIL-style (TODO-417 half 1): a header line —
    the reason-aware id/evidence line (unresolvable_reason leads, with
    evidence_label kept as a bracketed citation when present; other axes
    unchanged) — followed by the axis's own indented detail_fn lines, the
    same ones the FAIL/WARN branch uses, MINUS any detail line that just
    repeats the header verbatim (TODO-417 half 1 fix: peripheral/pullup_value's
    detail_fn leads with the same evidence text the header already carries).
  - the supply axis's identifier is refdes + pin + net (not refdes alone),
    so multiple UNRESOLVABLE pins on one component render as distinct lines.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import run_checks as rct


def _peripheral(net, severity, pins, evidence, kb_provenance=None):
    return {
        "net": net, "violation": "ROLE_MISMATCH", "severity": severity,
        "pins": pins, "evidence": evidence, "kb_provenance": kb_provenance or [],
    }


def test_i2c_swap_shape_two_fails_then_two_unresolvables():
    # The my_stm32_board_i2c_swap fixture's actual shape (LT-21 predicted output).
    report = {
        "peripheral_integrity_results": [
            _peripheral("/I2C2_SCL", "UNRESOLVABLE", [],
                        "Cannot fully verify net '/I2C2_SCL': MPN(s) not in KB: "
                        "['Conn_01x04_Pin'].", kb_provenance=["VENDOR_XML"]),
            _peripheral("/I2C2_SDA", "UNRESOLVABLE", [],
                        "Cannot fully verify net '/I2C2_SDA': MPN(s) not in KB: "
                        "['Conn_01x04_Pin'].", kb_provenance=["VENDOR_XML"]),
            _peripheral("/I2C2_SDA", "FAIL", ["U2.21"],
                        "Pin U2.21 (function 'PB10') is an I2C SCL pin but sits "
                        "on net '/I2C2_SDA' — SDA/SCL swap."),
            _peripheral("/I2C2_SCL", "FAIL", ["U2.22"],
                        "Pin U2.22 (function 'PB11') is an I2C SDA pin but sits "
                        "on net '/I2C2_SCL' — SDA/SCL swap."),
        ],
    }
    text = rct._render_findings(report)
    assert text.index("[FAIL] peripheral") < text.index("UNRESOLVABLE")
    assert text.count("[FAIL] peripheral") == 2
    assert "Pin U2.21 (function 'PB10')" in text          # evidence, verbatim
    assert "Pin U2.22 (function 'PB11')" in text
    # FAIL findings here have empty kb_provenance -> no KB sources line for them.
    fail_block = text[text.index("[FAIL] peripheral"):text.index("UNRESOLVABLE")]
    assert "KB sources" not in fail_block
    # UNRESOLVABLE entries render the full evidence string (one line each).
    assert "Cannot fully verify net '/I2C2_SCL'" in text
    assert "MPN(s) not in KB" in text.split("UNRESOLVABLE", 1)[1]
    assert "Conn_01x04_Pin" in text.split("UNRESOLVABLE", 1)[1]


def test_peripheral_kb_sources_line_appears_only_when_nonempty():
    report = {
        "peripheral_integrity_results": [
            _peripheral("/NET_A", "WARN", ["U1.1"], "some finding",
                        kb_provenance=["VENDOR_XML", "PICO_SDK"]),
            _peripheral("/NET_B", "WARN", ["U1.2"], "another finding",
                        kb_provenance=[]),
        ],
    }
    text = rct._render_findings(report)
    assert "KB sources: VENDOR_XML, PICO_SDK" in text
    lines = text.splitlines()
    b_idx = next(i for i, l in enumerate(lines) if "NET_B" in l)
    # No KB sources line directly follows NET_B's block (empty provenance).
    assert not any("KB sources" in l for l in lines[b_idx:b_idx + 3])


def test_pass_entries_never_itemized_individually():
    # S3: a PASS entry is still never rendered as its own line — it is only
    # counted into the trailing hidden-PASS note (see test_hidden_pass_note_*).
    report = {
        "power_supply_results": [
            {"refdes": "U1", "part_number": "X", "supply_pin": "VCC (pin 1)",
             "connected_net": "+3V3", "actual_voltage_v": 3.3, "rated_min_v": 3.0,
             "rated_max_v": 3.6, "rated_abs_max_v": 3.6, "status": "PASS",
             "confidence": "high", "evidence_label": "ok"},
        ],
    }
    text = rct._render_findings(report)
    assert "VCC (pin 1)" not in text
    assert "FINDINGS" not in text
    assert "UNRESOLVABLE" not in text


def test_fails_precede_warns_across_axes():
    report = {
        "peripheral_integrity_results": [
            _peripheral("/W", "WARN", ["U1.1"], "a warn finding"),
        ],
        "structural_integrity_results": [
            {"refdes": "U2", "part_number": "X", "pin": "GND (pin 1)",
             "connected_net": "GND", "pin_kind": "ground", "expected_kind": "ground_net",
             "status": "FAIL", "confidence": "high", "evidence_label": "bad ground",
             "finding_code": "STRUCT_X"},
        ],
    }
    text = rct._render_findings(report)
    assert text.index("[FAIL] structural") < text.index("[WARN] peripheral")
    assert "finding_code: STRUCT_X" in text


def test_s1_unresolvable_reason_precedes_citation_and_both_survive():
    # Signal-axis shape (step_08_checker), the only axis with a structured
    # unresolvable_reason today — the spi_swap_stm32 "driver power domain
    # ambiguous" finding (logs/recon_417_428.md Q3b).
    report = {
        "results": [
            {"net": "/CSb", "status": "UNRESOLVABLE",
             "driver": {"refdes": "U2"}, "receiver": {"refdes": "U1", "pin_name": "PA4"},
             "unresolvable_reason": "Driver voltage unknown — U2 power domain ambiguous "
                                     "(2 rail pin(s), 2 distinct voltage(s): [3.3, 5.0])",
             "evidence_label": "Cache-sourced — Table 36: I/O static characteristics "
                                "(not locally verified)",
             "confidence": "low"},
        ],
    }
    text = rct._render_findings(report)
    reason_idx = text.index("Driver voltage unknown")
    label_idx = text.index("Cache-sourced — Table 36")
    assert reason_idx < label_idx                                    # reason precedes citation
    assert "[Cache-sourced — Table 36: I/O static characteristics " \
           "(not locally verified)]" in text                          # citation kept, bracketed
    assert "U2 power domain ambiguous" in text                        # reason kept in full


def test_s2_supply_unresolvable_lines_distinct_by_pin():
    def supply(pin, net):
        return {"refdes": "U1", "part_number": "STM32F103C8Tx", "supply_pin": pin,
                "connected_net": net, "actual_voltage_v": None, "rated_min_v": None,
                "rated_max_v": None, "rated_abs_max_v": None, "status": "UNRESOLVABLE",
                "confidence": None,
                "evidence_label": "Net voltage not confirmed (not locally verified)"}
    report = {
        "power_supply_results": [
            supply("VBAT (pin 1)", "VBAT"),
            supply("VDD (pin 24)", "3V3"),
            supply("VDD (pin 36)", "3V3"),
            supply("VDD (pin 48)", "3V3"),
        ],
    }
    text = rct._render_findings(report)
    for line in ("supply U1 VBAT (pin 1) <- VBAT",
                 "supply U1 VDD (pin 24) <- 3V3",
                 "supply U1 VDD (pin 36) <- 3V3",
                 "supply U1 VDD (pin 48) <- 3V3"):
        assert line in text
    # four DISTINCT header lines — not the old id_field="refdes" collapse to
    # four identical "supply U1" lines.
    assert text.count("supply U1 ") == 4


def test_s3_hidden_pass_note_counts_across_axes_and_is_absent_at_zero():
    report = {
        "power_supply_results": [
            {"refdes": "U1", "part_number": "X", "supply_pin": "VCC (pin 1)",
             "connected_net": "+3V3", "actual_voltage_v": 3.3, "rated_min_v": 3.0,
             "rated_max_v": 3.6, "rated_abs_max_v": 3.6, "status": "PASS",
             "confidence": "high", "evidence_label": "ok"},
        ],
        "structural_integrity_results": [
            {"refdes": "U2", "part_number": "X", "pin": "GND (pin 1)",
             "connected_net": "GND", "status": "PASS", "confidence": "high",
             "evidence_label": "ok"},
        ],
    }
    text = rct._render_findings(report)
    assert "(2 PASS finding(s) not shown — full detail in the JSON report)" in text
    assert rct._render_findings({}) == ""                      # N == 0 -> no note
    assert "PASS finding" not in rct._render_findings({})


def test_s4_unresolvable_renders_fail_style_header_plus_detail():
    report = {
        "peripheral_integrity_results": [
            _peripheral("/I2C2_SCL", "UNRESOLVABLE", [],
                        "Cannot fully verify net '/I2C2_SCL': MPN(s) not in KB: "
                        "['Conn_01x04_Pin'].", kb_provenance=["VENDOR_XML"]),
        ],
    }
    text = rct._render_findings(report)
    unresolvable_block = text.split("UNRESOLVABLE", 1)[1]
    assert "  peripheral /I2C2_SCL —" in unresolvable_block             # header (S1/S2 line)
    assert "Cannot fully verify net '/I2C2_SCL'" in unresolvable_block  # evidence, in the header
    assert "    KB sources: VENDOR_XML" in unresolvable_block           # detail_fn's KB line, reused


def test_unresolvable_detail_line_dropped_when_it_repeats_the_header():
    # TODO-417 half 1 fix: peripheral's detail_fn leads with r["evidence"], the
    # exact text _unresolvable_line already put in the header (peripheral has no
    # separate unresolvable_reason) — that duplicate line must not render twice,
    # but detail_fn's non-duplicate "KB sources:" line must still render.
    evidence = ("Cannot fully verify net '/I2C2_SCL': MPN(s) not in KB: "
                "['Conn_01x04_Pin'].")
    report = {
        "peripheral_integrity_results": [
            _peripheral("/I2C2_SCL", "UNRESOLVABLE", [], evidence,
                        kb_provenance=["VENDOR_XML"]),
        ],
    }
    text = rct._render_findings(report)
    assert text.count(evidence) == 1                # rendered exactly once (header only)
    assert "KB sources: VENDOR_XML" in text
