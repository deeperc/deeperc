"""LT-15 Phase B (D2): an absent --corpus-dir must be fatal for corpus mode and
survivable for --board mode.

`--board` runs one explicitly-named netlist and never iterates the corpus, so an
absent corpus dir costs it nothing. The startup check used to `sys.exit(1)`
regardless, which meant the public export's own README demo -- a `--board`
invocation -- exited 1 on a fresh clone, since netlist_corpus/ is private and is
never shipped. Fixed to WARN + continue for `--board` only; corpus mode has
nothing to walk and keeps the hard exit.

Same shape as test_missing_datasheets_dir.py (LT-20), which made the identical
split for the datasheets dir one line further down.

The board fixture used here is deliberately the exported example, not a
netlist_corpus/ board: this test has to pass in the public tree, which is the
tree the bug was found in.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOARD = REPO / "examples/my_stm32_board_i2c_swap/my_stm32_board_i2c_swap.net"

# Isolate the subprocess from live L2 vendor credentials, so a --board run
# never places a real network call (hermeticity). The credential var names
# themselves are private-tier (steps.step_03_l2_ext, excluded from the public
# export) — this degrades to a no-op strip if that module isn't importable,
# which is exactly correct: a genuine public clone has no L2 seam to isolate.
try:
    from steps import step_03_l2_ext as _l2_ext
    _CREDENTIAL_ENV_VARS = _l2_ext.CREDENTIAL_ENV_VARS
except Exception:
    _CREDENTIAL_ENV_VARS = ()

_ISOLATED_ENV = {
    k: v for k, v in os.environ.items() if k not in _CREDENTIAL_ENV_VARS
}


def _run(corpus_dir, output_dir, board=None):
    cmd = [sys.executable, str(REPO / "run_checks.py"),
           "--corpus-dir", str(corpus_dir), "--skip-confirm",
           "--output-dir", str(output_dir)]
    if board is not None:
        cmd += ["--board", str(board)]
    return subprocess.run(cmd, capture_output=True, text=True, env=_ISOLATED_ENV)


def test_missing_corpus_dir_warns_and_proceeds_in_board_mode(tmp_path):
    missing = tmp_path / "no_such_corpus_dir"
    assert not missing.exists()

    r = _run(missing, tmp_path, board=BOARD)

    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    assert f"WARN: corpus dir not found: {missing}" in r.stderr
    assert "ERROR: corpus dir not found" not in r.stderr
    # The board actually ran -- the guard lets the pipeline through, it doesn't
    # just swallow the exit.
    assert (tmp_path / "reports" / "my_stm32_board_i2c_swap.json").exists()


def test_missing_corpus_dir_still_fatal_in_corpus_mode(tmp_path):
    """The other half of the split: without --board there is nothing to walk, so
    the hard exit is preserved exactly as it was."""
    missing = tmp_path / "no_such_corpus_dir"
    assert not missing.exists()

    r = _run(missing, tmp_path)

    assert r.returncode == 1, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert f"ERROR: corpus dir not found: {missing}" in r.stderr
    assert "WARN: corpus dir not found" not in r.stderr


def test_present_corpus_dir_unaffected_in_board_mode(tmp_path):
    """Sanity companion: with the dir present, neither message appears."""
    present = REPO / "netlist_corpus"
    if not present.exists():          # public tree -- nothing to compare against
        return
    r = _run(present, tmp_path, board=BOARD)

    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    assert "WARN: corpus dir not found" not in r.stderr
    assert "ERROR: corpus dir not found" not in r.stderr
