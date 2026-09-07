"""Step 07 (voltage confirmation) — the interactive-prompt path.

Covers the EOFError fix: confirm_voltages() must accept all inferred
voltages rather than crash with a raw traceback when stdin has no
interactive input available (piped or backgrounded run, --skip-confirm not
set). See CONTRIBUTING.md — this is a verdict-inert bug fix, not a checker
change.
"""
import builtins

import pytest

from steps.step_07_confirm import confirm_voltages


def _rail(net_name, voltage_v, source="deterministic", confidence="high"):
    return {
        "net_name": net_name,
        "voltage_v": voltage_v,
        "source": source,
        "confidence": confidence,
    }


def test_eof_on_input_accepts_all_inferred_voltages(monkeypatch, capsys):
    """A closed/non-TTY stdin must not raise — it should behave like pressing
    Enter (accept every inferred voltage), not crash the run."""
    def _raise_eof(*_args, **_kwargs):
        raise EOFError

    monkeypatch.setattr(builtins, "input", _raise_eof)

    rails = [_rail("+3V3", 3.3), _rail("+5V", 5.0)]
    result = confirm_voltages(rails, skip=False)

    assert result == {"+3V3": 3.3, "+5V": 5.0}
    # Should not have propagated EOFError.
    captured = capsys.readouterr()
    assert "accepting all inferred voltages" in captured.out


def test_normal_interactive_accept_all(monkeypatch):
    """Pressing Enter (empty line) still accepts all inferred voltages —
    unaffected by the EOFError guard."""
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: "")

    rails = [_rail("+3V3", 3.3)]
    result = confirm_voltages(rails, skip=False)

    assert result == {"+3V3": 3.3}


def test_normal_interactive_correction_still_applies(monkeypatch):
    """A real correction typed at the prompt still overrides the inferred
    value — the EOFError guard must not interfere with the normal path."""
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: "1=5.0")

    rails = [_rail("+3V3", 3.3)]
    result = confirm_voltages(rails, skip=False)

    assert result == {"+3V3": 5.0}


def test_skip_confirm_never_calls_input(monkeypatch):
    """--skip-confirm bypasses the prompt entirely — input() must not even
    be reachable, EOFError guard or not."""
    def _fail(*_args, **_kwargs):
        raise AssertionError("input() should not be called when skip=True")

    monkeypatch.setattr(builtins, "input", _fail)

    rails = [_rail("+3V3", 3.3)]
    result = confirm_voltages(rails, skip=True)

    assert result == {"+3V3": 3.3}
