"""examples_smoke.py — the per-fixture expected_verdict gate (make examples-smoke).

Covers the pure diff logic directly (no pipeline run needed), plus one
subprocess-level integration assertion that the real shipped examples/
fixtures currently pass their own gate -- a genuine regression pin: if a
future checker change silently moves a shipped fixture's verdict, this
test (and `make test`, which chains examples-smoke after pytest) catches
it, not just a human re-reading the README by hand.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import examples_smoke  # noqa: E402

try:
    from steps import step_03_l2_ext as _l2_ext
    _CREDENTIAL_ENV_VARS = _l2_ext.CREDENTIAL_ENV_VARS
except Exception:
    _CREDENTIAL_ENV_VARS = ()

_ISOLATED_ENV = {k: v for k, v in os.environ.items() if k not in _CREDENTIAL_ENV_VARS}


def test_diff_family_checks_reports_only_mismatched_keys():
    expected = {"pass": 5, "warn": 0, "fail": 0, "unresolvable": 0}
    actual = {"pass": 5, "warn": 0, "fail": 1, "unresolvable": 0}
    problems = examples_smoke.diff_family_checks("supply_checks", expected, actual)
    assert len(problems) == 1
    assert "supply_checks.fail" in problems[0]
    assert "expected 0" in problems[0]
    assert "got 1" in problems[0]


def test_diff_family_checks_agree_on_full_match():
    expected = {"pass": 2, "warn": 0, "fail": 2, "unresolvable": 0}
    assert examples_smoke.diff_family_checks("peripheral_checks", expected, expected) == []


def test_diff_family_checks_ignores_keys_not_in_expected():
    """A fixture's expected_verdict need not enumerate every key the live
    summary carries -- only what's named is checked."""
    expected = {"fail": 2}
    actual = {"fail": 2, "pass": 0, "warn": 0, "unresolvable": 99}
    assert examples_smoke.diff_family_checks("peripheral_checks", expected, actual) == []


def test_find_fixtures_discovers_every_shipped_example_with_exactly_one_net():
    fixtures = examples_smoke.find_fixtures()
    names = {name for name, _, _ in fixtures}
    # every example this repo ships as of this test's writing; a fixture
    # dropped or renamed should fail this, not silently vanish from the gate.
    expected_names = {
        "my_stm32_board_floating_gnd", "my_stm32_board_i2c_fixed",
        "my_stm32_board_i2c_swap", "my_stm32_board_output_conflict",
        "my_stm32_board_pullup_presence", "my_stm32_board_pullup_value",
        "my_stm32_board_supply_overvoltage", "stm32_spi_swap",
    }
    assert expected_names <= names


def test_find_fixtures_every_shipped_example_has_a_provenance_json():
    """Every shipped fixture should carry a provenance.json with an
    expected_verdict -- a fixture that doesn't is invisible to the gate,
    which is exactly the gap this smoke test exists to close."""
    fixtures = examples_smoke.find_fixtures()
    missing = [name for name, _, prov in fixtures if prov is None]
    assert missing == [], f"fixture(s) with no provenance.json: {missing}"
    no_expected = [name for name, _, prov in fixtures
                   if prov is not None and "expected_verdict" not in prov]
    assert no_expected == [], f"fixture(s) with no expected_verdict block: {no_expected}"


@pytest.mark.integration
def test_shipped_examples_pass_the_real_smoke_gate():
    """Integration-level: run the actual script against the real examples/
    tree. This is the regression pin -- if a checker change silently moves
    a shipped fixture's verdict, this fails."""
    result = subprocess.run(
        [sys.executable, str(REPO / "examples_smoke.py")],
        capture_output=True, text=True, timeout=120, env=_ISOLATED_ENV,
    )
    assert result.returncode == 0, (
        f"examples_smoke.py failed against the real examples/ tree:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "PASSED" in result.stdout
