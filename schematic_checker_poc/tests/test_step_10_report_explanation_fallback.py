"""TODO-369 — pins the step-10 Gemma-explanation-failure wording.

When Ollama is unreachable, `_explain`/`_supply_explain` must emit a benign,
user-facing WARNING line — never the raw `RuntimeError` text (which embeds
`ollama_client`'s urlopen error) — and the caller's FAIL verdict must be
unaffected: only the `explanation` string changes, never `status`.
"""
import logging
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps import step_10_report
from steps.step_10_report import build_report
from steps.step_08_checker import CheckResult
from steps.step_08b_supply_checker import SupplyCheckResult
from steps.step_08c_structural_checker import StructuralCheckResult
from llm import ollama_client

BENIGN_LINE = "[STEP 10] explanation text unavailable (local LLM not running) — verdicts unaffected"


def _raise_unreachable(*args, **kwargs):
    raise RuntimeError("Ollama unreachable: <urlopen error [Errno 111] Connection refused>")


def test_signal_explanation_fallback_is_benign_and_fail_survives(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(ollama_client, "generate", _raise_unreachable)
    monkeypatch.setattr(step_10_report, "_LLM_UNAVAILABLE_WARNED", False)

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
    # The canned text is the emitted evidence label (TODO-511 EXPLAIN-FIX-2).
    assert entry["explanation"] == entry["evidence_label"]
    assert entry["explanation_source"] == "canned"

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert warnings == [BENIGN_LINE]
    assert not any("urlopen" in m or "Ollama unreachable" in m for m in warnings)
    assert any("Ollama unreachable" in m for m in debugs)


def test_supply_explanation_fallback_is_benign_and_fail_survives(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(ollama_client, "generate", _raise_unreachable)
    monkeypatch.setattr(step_10_report, "_LLM_UNAVAILABLE_WARNED", False)

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
    # TODO-520 E4: the canned text is the emitted evidence_label (which may carry
    # tier/qualifier suffixes), not the raw label.
    assert entry["explanation"] == entry["evidence_label"]
    assert entry["explanation"].startswith("undervoltage")
    assert entry["explanation_source"] == "canned"

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert warnings == [BENIGN_LINE]
    assert not any("urlopen" in m or "Ollama unreachable" in m for m in warnings)
    assert any("Ollama unreachable" in m for m in debugs)


# ---------------------------------------------------------------------------
# TODO-520 — explanation_source, canned supply text == emitted label, once-per-run
# ---------------------------------------------------------------------------

_RAW = "Confirmed — §5.2 Electrical Characteristics"


def _sig(status="FAIL", reason=None):
    return CheckResult(
        net_name="/NET1", status=status, driver_refdes="U1", driver_voltage=5.0,
        driver_voltage_source="datasheet", receiver_refdes="U2", receiver_pin_name="PB6",
        receiver_VIH_max=3.6, receiver_abs_max=None, receiver_confidence="high",
        driver_confidence="high", combined_confidence="high",
        evidence_label="5.0V > 3.6V", unresolvable_reason=reason,
    )


def _sup(status="FAIL", label="undervoltage", part="AMS1117-3.3"):
    return SupplyCheckResult(
        refdes="U3", part_number=part, supply_pin_name="VIN", supply_pin_id="1",
        connected_net="+5V", actual_voltage=5.0, rated_min=4.5, rated_max=15.0,
        rated_abs_max=18.0, status=status, confidence="high",
        evidence_label=label, explanation=None,
    )


def _build(tmp_path, results=(), supply=(), meta=None, structural=None):
    kw = {"structural_results": structural} if structural is not None else {}
    return build_report(
        source_netlist="", results=list(results), confirmed_voltages={},
        extraction_metadata=meta or {}, output_path=str(tmp_path / "report.json"),
        supply_results=list(supply), **kw)


def _stale_meta(part):
    return {part: {"pdf_path": f"/x/{part}.pdf", "source_hash": "abc",
                   "cache_currency": "stale"}}


def _patch_gen(monkeypatch, fn):
    monkeypatch.setattr(ollama_client, "generate", fn)
    monkeypatch.setattr(step_10_report, "_LLM_UNAVAILABLE_WARNED", False)


def test_n1_signal_model_text_is_local_model(monkeypatch, tmp_path, caplog):
    _patch_gen(monkeypatch, lambda *a, **k: "model prose")
    with caplog.at_level(logging.DEBUG, logger="steps.step_10_report"):
        e = _build(tmp_path, results=[_sig()])["results"][0]
    assert e["explanation"] == "model prose"
    assert e["explanation_source"] == "local_model"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_n2_supply_model_text_is_local_model(monkeypatch, tmp_path):
    _patch_gen(monkeypatch, lambda *a, **k: "model prose")
    e = _build(tmp_path, supply=[_sup()])["power_supply_results"][0]
    assert e["explanation"] == "model prose"
    assert e["explanation_source"] == "local_model"
    assert e["evidence_label"] != "model prose"


def test_n3_pass_rows_have_no_explanation(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("generate must not be called for PASS rows")
    _patch_gen(monkeypatch, boom)
    rep = _build(tmp_path, results=[_sig("PASS")], supply=[_sup("PASS")])
    for e in (rep["results"][0], rep["power_supply_results"][0]):
        assert e["explanation"] is None
        assert e["explanation_source"] is None


def test_n4_signal_unresolvable_uses_reason_canned(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("generate must not be called for UNRESOLVABLE")
    _patch_gen(monkeypatch, boom)
    e = _build(tmp_path, results=[_sig("UNRESOLVABLE", reason="no datasheet")])["results"][0]
    assert e["explanation"] == "no datasheet"
    assert e["explanation_source"] == "canned"


def test_n5_canned_supply_text_is_tier_rewritten_label(monkeypatch, tmp_path):
    _patch_gen(monkeypatch, _raise_unreachable)
    e = _build(tmp_path, supply=[_sup(label=_RAW)],
               meta=_stale_meta("AMS1117-3.3"))["power_supply_results"][0]
    assert e["explanation"] == e["evidence_label"]
    assert e["explanation"].startswith("Cache-sourced — ")
    assert "Confirmed" not in e["explanation"]
    assert e["explanation_source"] == "canned"


def test_n6_prompt_never_sees_tier_rewrite(monkeypatch, tmp_path):
    prompts = []

    def rec(prompt, *a, **k):
        prompts.append(prompt)
        return "x"
    _patch_gen(monkeypatch, rec)
    _build(tmp_path, supply=[_sup(label=_RAW)], meta=_stale_meta("AMS1117-3.3"))
    assert len(prompts) == 1
    assert _RAW in prompts[0]
    assert "Cache-sourced" not in prompts[0]
    assert "(local datasheet differs from cache source)" not in prompts[0]


def test_n7_n8_warning_once_per_process(monkeypatch, tmp_path, caplog):
    _patch_gen(monkeypatch, _raise_unreachable)
    with caplog.at_level(logging.DEBUG, logger="steps.step_10_report"):
        rep = _build(tmp_path, results=[_sig()], supply=[_sup()])
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        debugs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert warnings == [BENIGN_LINE]
        assert BENIGN_LINE in debugs
        assert rep["results"][0]["explanation_source"] == "canned"
        assert rep["power_supply_results"][0]["explanation_source"] == "canned"
        caplog.clear()
        # N8: flag deliberately NOT reset
        _build(tmp_path, results=[_sig()], supply=[_sup()])
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_n9_schema_and_key_presence(monkeypatch, tmp_path):
    _patch_gen(monkeypatch, lambda *a, **k: "model prose")
    structural = [StructuralCheckResult(
        refdes="U2", part_number="PART-Y", pin_name="NRST", pin_id="7",
        pin_kind="input", connected_net="NRST", expected_kind="input",
        status="WARN", confidence="medium", evidence_label="x",
        explanation="e", finding_code="F1")]
    rep = _build(tmp_path, results=[_sig(), _sig("PASS")],
                 supply=[_sup(), _sup("PASS")], structural=structural)
    assert rep["schema_version"] == "poc-1.6"
    assert all("explanation_source" in r for r in rep["results"])
    assert all("explanation_source" in r for r in rep["power_supply_results"])
    assert rep["structural_integrity_results"]
    assert not any("explanation_source" in r for r in rep["structural_integrity_results"])


# ---------------------------------------------------------------------------
# TODO-511 Phase 5 — the model is asked only when the row's numbers make the
# prompt's claim true (supply FAIL; signal FAIL with driver > threshold)
# ---------------------------------------------------------------------------

def _recording_gen(monkeypatch, exc=None):
    calls = []

    def gen(*a, **k):
        calls.append((a, k))
        if exc is not None:
            raise exc
        return "MODEL TEXT"
    _patch_gen(monkeypatch, gen)
    return calls


def test_p5a_supply_warn_is_canned_without_model_call(monkeypatch, tmp_path):
    calls = _recording_gen(monkeypatch)
    e = _build(tmp_path, supply=[_sup("WARN")])["power_supply_results"][0]
    assert e["explanation"] == e["evidence_label"]
    assert e["explanation_source"] == "canned"
    assert calls == []


def test_p5b_supply_fail_asks_the_model(monkeypatch, tmp_path):
    calls = _recording_gen(monkeypatch)
    e = _build(tmp_path, supply=[_sup("FAIL")])["power_supply_results"][0]
    assert e["explanation"] == "MODEL TEXT"
    assert e["explanation_source"] == "local_model"
    assert len(calls) == 1


def test_p5c_supply_fail_model_error_is_canned_label(monkeypatch, tmp_path):
    calls = _recording_gen(monkeypatch, exc=RuntimeError("Ollama unreachable: x"))
    e = _build(tmp_path, supply=[_sup("FAIL")])["power_supply_results"][0]
    assert e["explanation"] == e["evidence_label"]
    assert e["explanation_source"] == "canned"
    assert len(calls) == 1


def test_p5d_signal_warn_is_emitted_label_without_model_call(monkeypatch, tmp_path):
    calls = _recording_gen(monkeypatch)
    e = _build(tmp_path, results=[_sig("WARN")])["results"][0]
    assert e["explanation"] == e["evidence_label"]
    assert e["explanation_source"] == "canned"
    assert calls == []


def test_p5e_signal_fail_driver_above_threshold_asks_the_model(monkeypatch, tmp_path):
    calls = _recording_gen(monkeypatch)
    e = _build(tmp_path, results=[_sig("FAIL")])["results"][0]
    assert e["explanation"] == "MODEL TEXT"
    assert e["explanation_source"] == "local_model"
    assert len(calls) == 1


def test_p5f_signal_fail_driver_not_above_threshold_is_canned(monkeypatch, tmp_path):
    calls = _recording_gen(monkeypatch)
    for volts in (3.6, 3.0):  # equal, and below
        r = _sig("FAIL")
        r.driver_voltage = volts
        e = _build(tmp_path, results=[r])["results"][0]
        assert e["explanation"] == e["evidence_label"]
        assert e["explanation_source"] == "canned"
    assert calls == []


def test_p5g_canned_text_identical_to_model_error_text(monkeypatch, tmp_path):
    calls = _recording_gen(monkeypatch)
    warn_sig = _build(tmp_path, results=[_sig("WARN")])["results"][0]
    warn_sup = _build(tmp_path, supply=[_sup("WARN")])["power_supply_results"][0]
    assert calls == []
    # Same rows with a model that raises: what the old code returned for WARN
    # when no model was reachable (the stub raising on FAIL rows is the
    # unreachable-model path; WARN rows are rebuilt as FAIL-shaped fallbacks).
    _recording_gen(monkeypatch, exc=RuntimeError("Ollama unreachable: x"))
    err_sig = _build(tmp_path, results=[_sig("FAIL")])["results"][0]
    err_sup = _build(tmp_path, supply=[_sup("FAIL", label="undervoltage")])["power_supply_results"][0]
    assert warn_sig["explanation"] == err_sig["explanation"]
    assert warn_sig["explanation_source"] == err_sig["explanation_source"]
    assert warn_sup["explanation"] == warn_sup["evidence_label"]
    assert err_sup["explanation"] == err_sup["evidence_label"]
    assert warn_sup["explanation_source"] == err_sup["explanation_source"] == "canned"


_BELOW_VIH_MIN = ("driver 1.2V is below receiver VIH_min 2.0V "
                  "— signal may not register as logic HIGH")


def test_p5h_signal_warn_below_vih_min_is_emitted_label(monkeypatch, tmp_path):
    calls = _recording_gen(monkeypatch)
    r = _sig("WARN")
    r.driver_voltage = 1.2
    r.evidence_label = _BELOW_VIH_MIN
    e = _build(tmp_path, results=[r])["results"][0]
    assert e["explanation"] == e["evidence_label"]
    assert e["explanation"].startswith(_BELOW_VIH_MIN)
    assert e["explanation_source"] == "canned"
    assert calls == []


def test_p5i_signal_fail_abs_max_below_driver_vih_max_above_is_emitted_label(monkeypatch, tmp_path):
    calls = _recording_gen(monkeypatch)
    r = _sig("FAIL")
    r.receiver_abs_max = 4.0
    r.receiver_VIH_max = 5.5  # the old sentence took this first and read "rated 5.5V max"
    r.driver_voltage = 5.0
    r.evidence_label = "driver 5.0V exceeds receiver absolute max 4.0V"
    # driver (5.0) is not above the guard's threshold (VIH_max 5.5) -> not asked
    e = _build(tmp_path, results=[r])["results"][0]
    assert e["explanation"] == e["evidence_label"]
    assert "5.5" not in e["explanation"]
    assert e["explanation_source"] == "canned"
    assert calls == []


def _sig_with_receiver_part(label=_RAW):
    r = _sig("FAIL")
    r.evidence_label = label
    comps = [types.SimpleNamespace(refdes="U2", part_number="AMS1117-3.3")]
    return r, comps


def _build_with_part(tmp_path, r, comps):
    return build_report(
        source_netlist="", results=[r], confirmed_voltages={},
        extraction_metadata=_stale_meta("AMS1117-3.3"),
        output_path=str(tmp_path / "report.json"), components=comps)


def test_p5j_signal_model_error_returns_emitted_not_raw_label(monkeypatch, tmp_path):
    _recording_gen(monkeypatch, exc=RuntimeError("Ollama unreachable: x"))
    r, comps = _sig_with_receiver_part()
    e = _build_with_part(tmp_path, r, comps)["results"][0]
    assert e["explanation"] == e["evidence_label"]
    assert e["explanation"] != _RAW
    assert e["explanation"].startswith("Cache-sourced — ")
    assert e["explanation_source"] == "canned"


def test_p5k_signal_prompt_has_raw_numbers_not_emitted_label(monkeypatch, tmp_path):
    prompts = []

    def rec(prompt, *a, **k):
        prompts.append(prompt)
        return "x"
    _patch_gen(monkeypatch, rec)
    r, comps = _sig_with_receiver_part()
    e = _build_with_part(tmp_path, r, comps)["results"][0]
    assert e["evidence_label"] != _RAW
    assert len(prompts) == 1
    assert "5.0" in prompts[0] and "3.6" in prompts[0]
    assert "Cache-sourced" not in prompts[0]
    assert e["evidence_label"] not in prompts[0]
