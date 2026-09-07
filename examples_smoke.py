#!/usr/bin/env python3
"""
examples_smoke.py — verify every examples/*/provenance.json fixture still
produces the findings it documents.

Runs each example board through run_checks.py --board (fresh, isolated
--output-dir per fixture), loads the resulting JSON report, and diffs the
board's classified status plus each checker-family bucket named in the
fixture's provenance.json "expected_verdict" block against the live result.

Only the structured fields (board_status + <family>_checks pass/warn/fail/
unresolvable counts) are diffed -- the prose fields (unrelated_unresolvables,
unchanged_axes, confidence) are documentation for a human reader, not
mechanically checked here.

A fixture without a provenance.json, or one whose provenance.json lacks an
"expected_verdict" block, is reported and skipped rather than silently
ignored -- CONTRIBUTING.md's fixture-honesty principle applies to the smoke
gate itself.

Usage:
    python3 examples_smoke.py
Exit code 0 if every fixture's actual output matches its documented
expected_verdict; 1 otherwise.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent
EXAMPLES_DIR = REPO_ROOT / "examples"
POC_DIR = REPO_ROOT / "schematic_checker_poc"
sys.path.insert(0, str(POC_DIR))

from pipeline import classify_report  # noqa: E402


def find_fixtures():
    """One (net_file, provenance_json) pair per examples/*/ subdirectory that
    has exactly one .net file. Directories without a provenance.json are
    still yielded (provenance=None) so they're reported, not silently
    skipped."""
    fixtures = []
    for d in sorted(EXAMPLES_DIR.iterdir()):
        if not d.is_dir():
            continue
        nets = list(d.glob("*.net"))
        if len(nets) != 1:
            continue
        prov_path = d / "provenance.json"
        provenance = json.loads(prov_path.read_text()) if prov_path.exists() else None
        fixtures.append((d.name, nets[0], provenance))
    return fixtures


def run_board(net_path: Path, out_dir: Path) -> dict:
    cmd = [sys.executable, str(REPO_ROOT / "run_checks.py"),
           "--board", str(net_path), "--skip-confirm",
           "--output-dir", str(out_dir)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"run_checks.py exited {result.returncode}\n"
                            f"stdout: {result.stdout}\nstderr: {result.stderr}")
    report_path = out_dir / "reports" / f"{net_path.stem}.json"
    if not report_path.exists():
        raise RuntimeError(f"expected report not written: {report_path}\n"
                            f"stdout: {result.stdout}\nstderr: {result.stderr}")
    return json.loads(report_path.read_text())


def diff_family_checks(name: str, expected: dict, actual: dict) -> list[str]:
    """Compares only the keys present in `expected` -- a fixture's
    provenance.json need not enumerate every key the live summary carries."""
    problems = []
    for key, expected_val in expected.items():
        actual_val = actual.get(key)
        if actual_val != expected_val:
            problems.append(f"    {name}.{key}: expected {expected_val}, got {actual_val}")
    return problems


def main() -> int:
    fixtures = find_fixtures()
    any_failure = False
    any_skipped = False

    print(f"examples_smoke: {len(fixtures)} fixture(s) found under examples/\n")

    for fixture_name, net_path, provenance in fixtures:
        print(f"=== {fixture_name} ===")
        if provenance is None:
            print("  SKIP: no provenance.json")
            any_skipped = True
            continue
        expected = provenance.get("expected_verdict")
        if expected is None:
            print("  SKIP: provenance.json has no \"expected_verdict\" block")
            any_skipped = True
            continue

        with tempfile.TemporaryDirectory() as tmp:
            try:
                report = run_board(net_path, Path(tmp))
            except Exception as e:
                print(f"  FAIL: could not run board: {e}")
                any_failure = True
                continue

        problems = []
        actual_status = classify_report(report)
        expected_status = expected.get("board_status")
        if expected_status is not None and actual_status != expected_status:
            problems.append(f"    board_status: expected {expected_status!r}, "
                             f"got {actual_status!r}")

        summary = report.get("summary", {})
        for key, expected_val in expected.items():
            if not key.endswith("_checks") or not isinstance(expected_val, dict):
                continue
            actual_val = summary.get(key, {})
            problems.extend(diff_family_checks(key, expected_val, actual_val))

        if problems:
            print("  FAIL:")
            for p in problems:
                print(p)
            any_failure = True
        else:
            print("  OK")

    print()
    if any_failure:
        print("examples_smoke: FAILED — one or more fixtures drifted from their "
              "documented expected_verdict.")
        return 1
    if any_skipped:
        print("examples_smoke: PASSED (with skips) — every checked fixture matched; "
              "some fixtures have no expected_verdict to check against.")
        return 0
    print("examples_smoke: PASSED — every fixture matches its documented expected_verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
