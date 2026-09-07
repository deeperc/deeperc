"""run_checks.py --verbose: raises logging from the runner's WARNING default to
INFO, matching main.py's single-netlist path.

main.py defaults to INFO; run_checks.py defaults to WARNING (to keep full
corpus runs quiet) and, until this flag, had no way to raise it back for a
single --board run — so info-level diagnostics (e.g. the extraction-time
advisory plausibility guard named in LIMITATIONS.md) were invisible under the
README's documented demo entrypoint. See CONTRIBUTING.md: this is a
verdict-inert CLI/logging change, not a checker change.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOARD = REPO / "examples/my_stm32_board_i2c_swap/my_stm32_board_i2c_swap.net"

try:
    from steps import step_03_l2_ext as _l2_ext
    _CREDENTIAL_ENV_VARS = _l2_ext.CREDENTIAL_ENV_VARS
except Exception:
    _CREDENTIAL_ENV_VARS = ()

_ISOLATED_ENV = {
    k: v for k, v in os.environ.items() if k not in _CREDENTIAL_ENV_VARS
}


def _run(tmp_path, verbose):
    cmd = [sys.executable, str(REPO / "run_checks.py"),
           "--board", str(BOARD), "--skip-confirm",
           "--output-dir", str(tmp_path)]
    if verbose:
        cmd.append("--verbose")
    return subprocess.run(cmd, capture_output=True, text=True, env=_ISOLATED_ENV)


def test_default_verbosity_suppresses_info_level_report_written_line(tmp_path):
    r = _run(tmp_path, verbose=False)
    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    # PIPELINE.md documents this exact line as "logged below the default
    # threshold and does not print" -- it's the info-level marker this test
    # pins on.
    assert "[STEP 10] Report written to" not in r.stderr

def test_verbose_flag_surfaces_info_level_report_written_line(tmp_path):
    r = _run(tmp_path, verbose=True)
    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    assert "[STEP 10] Report written to" in r.stderr
