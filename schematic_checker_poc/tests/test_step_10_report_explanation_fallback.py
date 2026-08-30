"""TODO-369 — pins the step-10 Gemma-explanation-failure wording.

When Ollama is unreachable, `_explain`/`_supply_explain` must emit a benign,
user-facing WARNING line — never the raw `RuntimeError` text (which embeds
`ollama_client`'s urlopen error) — and the caller's FAIL verdict must be
unaffected: only the `explanation` string changes, never `status`.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.step_10_report import build_report
from steps.step_08_checker import CheckResult
from steps.step_08b_supply_checker import SupplyCheckResult
from llm import ollama_client

BENIGN_LINE = "[STEP 10] explanation text unavailable (local LLM not running) — verdicts unaffected"


def _raise_unreachable(*args, **kwargs):
    raise RuntimeError("Ollama unreachable: <urlopen error [Errno 111] Connection refused>")


def test_signal_explanation_fallback_is_benign_and_fail_survives(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(ollama_client, "generate", _raise_unreachable)

    result = CheckResult(
        net_name="/NET1",
        status="FAIL",
        driver_refdes="U1",
        driver_voltage=5.0,
        driver_voltage_source="datasheet",
        receiver_refdes="U2",
        receiver_pin_name="PB6",
        receiver_VIH_max=3.6,
        receiver_abs_max=None,
        receiver_confidence="high",
        driver_confidence="high",
        combined_confidence="high",
        evidence_label="5.0V > 3.6V",
        unresolvable_reason=None,
    )

    with caplog.at_level(logging.DEBUG, logger="steps.step_10_report"):
        report = build_report(
            source_netlist="", results=[result], confirmed_voltages={},
            extraction_metadata={}, output_path=str(tmp_path / "report.json"),
        )

    entry = report["results"][0]
    assert entry["status"] == "FAIL"
    # Fallback content is unchanged (SCOPE: zero behavior change) — only the
    # WARNING log wording moves; the report's explanation field is untouched.
    assert entry["explanation"] == "U1 drives 5.0V into PB6 which is rated 3.6V max."

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert warnings == [BENIGN_LINE]
    assert not any("urlopen" in m or "Ollama unreachable" in m for m in warnings)
    assert any("Ollama unreachable" in m for m in debugs)


def test_supply_explanation_fallback_is_benign_and_fail_survives(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(ollama_client, "generate", _raise_unreachable)

    supply_result = SupplyCheckResult(
        refdes="U3",
        part_number="AMS1117-3.3",
        supply_pin_name="VIN",
        supply_pin_id="1",
        connected_net="+5V",
        actual_voltage=5.0,
        rated_min=4.5,
        rated_max=15.0,
        rated_abs_max=18.0,
        status="FAIL",
        confidence="high",
        evidence_label="undervoltage",
        explanation=None,
    )

    with caplog.at_level(logging.DEBUG, logger="steps.step_10_report"):
        report = build_report(
            source_netlist="", results=[], confirmed_voltages={},
            extraction_metadata={}, output_path=str(tmp_path / "report.json"),
            supply_results=[supply_result],
        )

    entry = report["power_supply_results"][0]
    assert entry["status"] == "FAIL"
    # Fallback content is unchanged (SCOPE: zero behavior change) — only the
    # WARNING log wording moves; the report's explanation field is untouched.
    assert entry["explanation"] == "undervoltage"

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert warnings == [BENIGN_LINE]
    assert not any("urlopen" in m or "Ollama unreachable" in m for m in warnings)
    assert any("Ollama unreachable" in m for m in debugs)
