"""LT-21: the --board mode FINDINGS section (run_checks._render_findings).

Presentation-only, hermetic (a synthetic report dict, no pipeline run) — same
convention as test_corpus_result_dict.py. Locks:
  - FAILs render before WARNs, both before the compact UNRESOLVABLE lines.
  - PASS entries are never itemized.
  - peripheral findings print the evidence string VERBATIM, plus a
    'KB sources: ...' line only when kb_provenance is non-empty.
  - UNRESOLVABLE entries are one line each, full evidence string.
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


def test_pass_entries_never_itemized():
    report = {
        "power_supply_results": [
            {"refdes": "U1", "part_number": "X", "supply_pin": "VCC (pin 1)",
             "connected_net": "+3V3", "actual_voltage_v": 3.3, "rated_min_v": 3.0,
             "rated_max_v": 3.6, "rated_abs_max_v": 3.6, "status": "PASS",
             "confidence": "high", "evidence_label": "ok"},
        ],
    }
    assert rct._render_findings(report) == ""


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
