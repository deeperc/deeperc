"""Grader for TODO-524: main.py discloses unevaluated checks under its verdict.

Authored by Claude.ai as a verbatim grader for a C-flow run (plan section 4:
tests are ADDED as verbatim files, never authored by CC).

Contract under test:
  * pipeline.count_unresolvable(report) -> int counts "UNRESOLVABLE" over the
    same VERDICT_MOVING statuses classify_report reads, and is 0 for a
    pipeline_error report.
  * main.py prints, on the line right after `[VERDICT] <status>` and on the same
    stream, exactly
        [UNRESOLVABLE] {n} finding(s) not evaluated; see LIMITATIONS.md (board verdicts)
    iff n > 0, for every verdict. The [VERDICT] line itself, the verdict and
    the exit code are unchanged, and it stays the only line starting with
    "[VERDICT]".
"""

import subprocess
import sys
from pathlib import Path

import pytest

POC = Path(__file__).resolve().parent.parent
REPO = POC.parent
sys.path.insert(0, str(POC))

import main as main_mod  # noqa: E402
import pipeline  # noqa: E402
from steps import checker_registry as reg  # noqa: E402

QUALIFIER = "[UNRESOLVABLE] {n} finding(s) not evaluated; see LIMITATIONS.md (board verdicts)"

I2C_SWAP_BOARD = REPO / "examples" / "my_stm32_board_i2c_swap" / "my_stm32_board_i2c_swap.net"
I2C_FIXED_BOARD = REPO / "examples" / "my_stm32_board_i2c_fixed" / "my_stm32_board_i2c_fixed.net"


def _report(signal=(), supply=(), peripheral=(), pullup=(), **extra):
    """Build a minimal report. signal/supply carry `status`; peripheral and
    pullup_value carry `severity` (the registry's two status readers)."""
    r = {
        "results": [{"status": s} for s in signal],
        "power_supply_results": [{"status": s} for s in supply],
        "peripheral_integrity_results": [{"severity": s} for s in peripheral],
        "pullup_value_results": [{"severity": s} for s in pullup],
    }
    r.update(extra)
    return r


# -- count_unresolvable --

@pytest.mark.parametrize("report, expected", [
    (_report(supply=["PASS"], peripheral=["UNRESOLVABLE", "UNRESOLVABLE"]), 2),
    (_report(peripheral=["UNRESOLVABLE"] * 3), 3),
    (_report(supply=["FAIL"], signal=["UNRESOLVABLE"]), 1),
    (_report(supply=["WARN"], pullup=["UNRESOLVABLE"], peripheral=["UNRESOLVABLE"]), 2),
    (_report(signal=["unresolvable"]), 1),          # status reader upper-cases
    (_report(supply=["PASS", "PASS"]), 0),
    (_report(), 0),
    ({}, 0),
])
def test_count_unresolvable_matrix(report, expected):
    assert pipeline.count_unresolvable(report) == expected


def test_count_unresolvable_pipeline_error_is_zero():
    r = _report(peripheral=["UNRESOLVABLE"], pipeline_error="boom")
    assert pipeline.count_unresolvable(r) == 0


def test_count_ignores_non_verdict_moving_keys():
    r = _report(supply=["PASS"])
    r["summary"] = {"total_unresolvable": 9, "unresolvable": 9}
    r["not_a_checker_bucket"] = [{"status": "UNRESOLVABLE"}, {"severity": "UNRESOLVABLE"}]
    assert pipeline.count_unresolvable(r) == 0


def test_count_agrees_with_verdict_moving_extraction():
    """Same statuses list classify_report builds: one bucket per VERDICT_MOVING spec."""
    r = {}
    for i, spec in enumerate(reg.VERDICT_MOVING):
        r[spec.report_bucket] = [{"status": "UNRESOLVABLE", "severity": "UNRESOLVABLE"}] * (i + 1)
    expected = sum(i + 1 for i in range(len(reg.VERDICT_MOVING)))
    assert pipeline.count_unresolvable(r) == expected


def test_classify_report_precedence_unchanged():
    r = _report(supply=["PASS"], peripheral=["UNRESOLVABLE"] * 5)
    assert pipeline.classify_report(r) == "all_pass"
    assert pipeline.classify_report(_report(peripheral=["UNRESOLVABLE"])) == "has_unresolvable_only"


# -- main.py console line (hermetic: run_board is patched) --

def _run_main(monkeypatch, capsys, tmp_path, report):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline, "run_board", lambda ctx: report)
    monkeypatch.setattr(sys, "argv", ["main.py", "--netlist", "x.net", "--skip-confirm"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    out, err = capsys.readouterr()
    return exc.value.code, out.splitlines(), err


@pytest.mark.parametrize("report, verdict, n, code", [
    (_report(supply=["PASS"], peripheral=["UNRESOLVABLE", "UNRESOLVABLE"]), "all_pass", 2, 0),
    (_report(supply=["FAIL"], peripheral=["UNRESOLVABLE"]), "has_fail", 1, 1),
    (_report(supply=["WARN"], peripheral=["UNRESOLVABLE"] * 4), "has_warn", 4, 0),
    (_report(peripheral=["UNRESOLVABLE"] * 3), "has_unresolvable_only", 3, 0),
])
def test_qualifier_printed_when_n_positive(monkeypatch, capsys, tmp_path, report, verdict, n, code):
    rc, lines, err = _run_main(monkeypatch, capsys, tmp_path, report)
    assert rc == code
    assert lines[-2:] == [f"[VERDICT] {verdict}", QUALIFIER.format(n=n)]
    assert "[UNRESOLVABLE]" not in err


@pytest.mark.parametrize("report, verdict, code", [
    (_report(supply=["PASS"]), "all_pass", 0),
    (_report(supply=["FAIL"]), "has_fail", 1),
    (_report(), "no_checkable_nets", 0),
])
def test_no_qualifier_when_n_zero(monkeypatch, capsys, tmp_path, report, verdict, code):
    rc, lines, err = _run_main(monkeypatch, capsys, tmp_path, report)
    assert rc == code
    assert lines[-1] == f"[VERDICT] {verdict}"
    assert not any(l.startswith("[UNRESOLVABLE]") for l in lines)
    assert "[UNRESOLVABLE]" not in err


def test_verdict_line_is_the_only_verdict_prefixed_line(monkeypatch, capsys, tmp_path):
    rc, lines, _ = _run_main(monkeypatch, capsys, tmp_path,
                             _report(supply=["PASS"], peripheral=["UNRESOLVABLE"]))
    assert [l for l in lines if l.startswith("[VERDICT]")] == ["[VERDICT] all_pass"]
    assert sum(l.startswith("[UNRESOLVABLE]") for l in lines) == 1


def test_pipeline_failure_prints_neither_line(monkeypatch, capsys, tmp_path):
    failure = pipeline.PipelineFailure(stage="02", kind="parse", detail="ERROR: nope")
    rc, lines, err = _run_main(monkeypatch, capsys, tmp_path, failure)
    assert rc == 1
    assert not any(l.startswith("[VERDICT]") or l.startswith("[UNRESOLVABLE]") for l in lines)
    assert "[STEP 02] ERROR: nope" in err


# -- end to end on the two shipped fixtures (cache-only, same pattern as
#    test_pipeline_failure.py's main.py subprocess tests) --

def _run_fixture(tmp_path, board):
    return subprocess.run(
        [sys.executable, str(POC / "main.py"), "--netlist", str(board), "--skip-confirm"],
        capture_output=True, text=True, cwd=tmp_path)


@pytest.mark.skipif(not I2C_SWAP_BOARD.exists(), reason="i2c-swap demo fixture not on disk")
def test_fixture_i2c_swap_prints_qualifier(tmp_path):
    r = _run_fixture(tmp_path, I2C_SWAP_BOARD)
    assert r.returncode == 1
    assert r.stdout.splitlines()[-2:] == ["[VERDICT] has_fail", QUALIFIER.format(n=2)]


@pytest.mark.skipif(not I2C_FIXED_BOARD.exists(), reason="i2c-fixed fixture not on disk")
def test_fixture_i2c_fixed_prints_qualifier(tmp_path):
    r = _run_fixture(tmp_path, I2C_FIXED_BOARD)
    assert r.returncode == 0
    assert r.stdout.splitlines()[-2:] == ["[VERDICT] all_pass", QUALIFIER.format(n=2)]
