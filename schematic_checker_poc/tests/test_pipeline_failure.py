"""Typed PipelineFailure + its three per-driver surfaces (Phase 4, spec §3.4).

Fixture: the parse-error board from the driver-agreement recon (STM32F030K6.kicad_sch —
a schematic source, not a .net export → step_02 raises). It is asserted through all three
driver presentations of the SAME unified failure:

  * SEMANTIC   — run_board returns a typed PipelineFailure(kind="parse", stage="02")
  * SURFACE 1  — main.py: stderr `[STEP 02] …` + exit 1, no report
  * SURFACE 2  — corpus full-run: eligibility catches the parse (board is skipped)
  * SURFACE 3  — corpus --board: D7b — surfaces the failure + exits NONZERO
                 (it used to print `0 PASS 0 WARN 0 FAIL` and exit 0)
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
POC = REPO / "schematic_checker_poc"
sys.path.insert(0, str(POC))
sys.path.insert(0, str(REPO))

import pipeline
import run_checks as rct

PARSE_BOARD = next(REPO.glob("netlist_corpus/**/STM32F030K6.kicad_sch"), None)
pytestmark = pytest.mark.skipif(PARSE_BOARD is None, reason="parse-error fixture board not on disk")

# TODO-426: main.py's exit code must gate on the cross-axis classify_report() verdict,
# not just the signal-only (step_08) fail bucket — my_stm32_board_i2c_swap has a
# peripheral-only FAIL (SDA/SCL swap) with a zero signal-bucket fail count, so the old
# `report["summary"]["fail"]` check exited 0 on a has_fail board.
I2C_SWAP_BOARD = REPO / "examples" / "my_stm32_board_i2c_swap" / "my_stm32_board_i2c_swap.net"
ALL_PASS_BOARD = (REPO / "netlist_corpus" / "esp32" / "olimex" / "ESP32-EVB" /
                  "HARDWARE" / "REV-F" / "ESP32-EVB_Rev_F.net")


def test_run_board_returns_typed_parse_failure():
    ctx = pipeline.PipelineContext(netlist_path=str(PARSE_BOARD), skip_confirm=True)
    outcome = pipeline.run_board(ctx)
    assert isinstance(outcome, pipeline.PipelineFailure)
    assert outcome.kind == "parse"
    assert outcome.stage == "02"
    assert outcome.detail.startswith("ERROR:")
    assert "kicad_sch" in outcome.detail.lower() or "not a netlist" in outcome.detail.lower()


def test_surface2_corpus_fullrun_eligibility_skips_it():
    eligible, resolved, blocking, nonblocking = rct.is_eligible(
        PARSE_BOARD, REPO / "netlist_corpus" / "datasheets")
    assert eligible is False
    assert any("PARSE_ERROR" in b for b in blocking)


def test_run_one_maps_failure_to_pipeline_error(tmp_path):
    result = rct.run_one(
        netlist_path=PARSE_BOARD, corpus_dir=REPO / "netlist_corpus",
        datasheets_dir=REPO / "netlist_corpus" / "datasheets",
        output_path=tmp_path / "r.json", skip_confirm=True)
    assert result["status"] == "pipeline_error"
    assert result["failed_at_step"] == "step_02"


def test_surface1_main_py_exit1_stderr(tmp_path):
    r = subprocess.run(
        [sys.executable, str(POC / "main.py"), "--netlist", str(PARSE_BOARD), "--skip-confirm"],
        capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 1
    assert "[STEP 02]" in r.stderr
    assert not (tmp_path / "report.json").exists()


def test_surface3_corpus_board_d7b_exits_nonzero(tmp_path):
    r = subprocess.run(
        [sys.executable, str(REPO / "run_checks.py"), "--board", str(PARSE_BOARD),
         "--skip-confirm", "--output-dir", str(tmp_path)],
        capture_output=True, text=True)
    assert r.returncode != 0, "D7b: --board must exit nonzero on a pipeline failure"
    assert "PIPELINE ERROR" in r.stderr
    # and it must NOT emit the misleading all-zero success line
    assert "0 PASS  0 WARN  0 FAIL" not in r.stdout


@pytest.mark.skipif(not I2C_SWAP_BOARD.exists(), reason="i2c-swap demo fixture not on disk")
def test_main_py_exits_1_on_cross_axis_has_fail(tmp_path):
    # my_stm32_board_i2c_swap's fail lives in the peripheral bucket, not the signal
    # bucket — the old `summary["fail"]` gate exited 0 here (the TODO-426 defect).
    r = subprocess.run(
        [sys.executable, str(POC / "main.py"), "--netlist", str(I2C_SWAP_BOARD),
         "--skip-confirm"],
        capture_output=True, text=True, cwd=tmp_path)
    assert "has_fail" in r.stdout
    assert r.returncode == 1


@pytest.mark.skipif(not ALL_PASS_BOARD.exists(), reason="all_pass fixture not on disk")
def test_main_py_exits_0_on_all_pass(tmp_path):
    r = subprocess.run(
        [sys.executable, str(POC / "main.py"), "--netlist", str(ALL_PASS_BOARD),
         "--skip-confirm"],
        capture_output=True, text=True, cwd=tmp_path)
    assert "all_pass" in r.stdout
    assert r.returncode == 0
