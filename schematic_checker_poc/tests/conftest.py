"""TODO-320: suite-level production-tree tripwire.

TODO-227 Phase 2b found two pre-existing tests (test_resolve_memo.py's
test_shared_doc_not_mutated_by_real_consumers, test_step_timing.py's _run())
that incidentally wrote real files into production datasheets_parsed/ during
`make test`, by exercising the real pipeline against a real board without
isolating PARSED_DIR (fixed in 5fbcafa + 5c8cc9c). This module makes that
class of leak structurally impossible to miss: a session-wide, autouse
fixture snapshots both production trees before any test runs and re-snapshots
after the last one finishes, failing the whole run if anything changed.

Design split: the snapshot/diff/setup/teardown logic lives in plain,
independently-testable functions (_snapshot_tree, _diff_snapshots,
_tripwire_setup, _tripwire_teardown); the pytest fixture itself is a thin
generator wrapping them, so this module's own tests (test_production_tree_
tripwire.py) can exercise the real logic against isolated tmp trees without
touching pytest fixture machinery or real production dirs.

Fail-reporting mechanism: pytest_sessionfinish, NOT pytest.fail() inside the
fixture's teardown. A session-scoped autouse fixture is finalized as part of
whichever test happens to be the last one collected to use it — raising
there attributes the failure to that arbitrary test ("ERROR at teardown of
test_whatever_ran_last"), which is misleading (this is a whole-run invariant,
not that test's fault) and entangles this check with pytest's per-test
teardown-error handling. pytest_sessionfinish fires once, after every test
AND every fixture teardown (including this one) has already completed, and
lets us set session.exitstatus directly and print one clear, dedicated
failure block — a much better fit for a suite-level invariant.
"""
import os
import sys
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
POC = REPO / "schematic_checker_poc"
sys.path.insert(0, str(POC))
sys.path.insert(0, str(REPO))

import pipeline  # noqa: E402

SKIP_ENV_VAR = "SCHECKER_SKIP_TREE_TRIPWIRE"

# Populated by the fixture's teardown; read by pytest_sessionfinish. Module-level
# because pytest_sessionfinish has no other reliable way to reach a session-scoped
# fixture's result once that fixture has already been torn down.
_tripwire_result = {"diff": None}


def _tripwire_roots() -> dict:
    """Tree roots resolved from pipeline's own constants (not hard-coded paths),
    so this fixture stays correct if those constants ever move."""
    return {
        "datasheets_parsed": Path(pipeline.DEFAULT_PARSED_DIR),
        "netlist_corpus_datasheets": Path(pipeline.DEFAULT_DATASHEETS_DIR),
        # TODO-386 Phase 3: the staging tier is a THIRD guarded root. It has to
        # be, because that phase made staging the DEFAULT destination of every
        # fresh extraction (R-C) — so the exact leak class this fixture exists
        # to catch (a test exercising the real extract path without isolating
        # its cache root) now lands in datasheets_staged/ instead of
        # datasheets_parsed/, where nothing was watching. Three tests leaked
        # into the real staging tree this way before their fixtures were
        # isolated; artifacts preserved in
        # investigation/todo386_staging_testleak_backup_20260818/.
        #
        # This guards CHANGE, not presence: a populated-but-untouched staging
        # tree (the normal state after a real run) snapshots identically before
        # and after and passes, so `make test` stays green alongside a populated
        # staging tier.
        "datasheets_staged": Path(pipeline.DEFAULT_STAGING_DIR),
        # LT-37 Phase 2: a FOURTH guarded root, off the production trees'
        # pattern entirely -- /tmp/deeperc_public_release is the Phase 4
        # public-release candidate directory (CLAUDE.md: never touch it).
        # No test should ever write there; _snapshot_tree already handles a
        # nonexistent root as empty, so watching it costs nothing when it's
        # absent (the common case) and catches any leak into it otherwise.
        "deeperc_public_release": Path("/tmp/deeperc_public_release"),
    }


def _snapshot_tree(root) -> dict:
    """relpath -> (size, mtime_ns) for every file under root. (relpath, size,
    mtime_ns) only, per TODO-320 scope -- no content hashing (too slow
    corpus-wide). Missing root snapshots as empty, not an error (a fresh
    checkout may not have datasheets_parsed/ yet)."""
    root = Path(root)
    snap = {}
    if not root.exists():
        return snap
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            try:
                st = fpath.stat()
            except OSError:
                continue
            relpath = str(fpath.relative_to(root))
            snap[relpath] = (st.st_size, st.st_mtime_ns)
    return snap


def _diff_snapshots(before: dict, after: dict) -> tuple:
    """(added, removed, changed) relpath lists, each sorted. changed = same
    relpath present in both snapshots but with a different (size, mtime_ns)."""
    before_keys = set(before)
    after_keys = set(after)
    added = sorted(after_keys - before_keys)
    removed = sorted(before_keys - after_keys)
    changed = sorted(
        relpath for relpath in (before_keys & after_keys)
        if before[relpath] != after[relpath]
    )
    return added, removed, changed


def _print_skip_warning() -> None:
    banner = (
        "\n" + "!" * 78 + "\n"
        "SCHECKER_SKIP_TREE_TRIPWIRE=1 -- TODO-320 production-tree tripwire "
        "DISABLED.\n"
        "Writes into datasheets_parsed/ or netlist_corpus/datasheets/ during this\n"
        "run will NOT be detected. Intended only for data-cycle tooling that\n"
        "legitimately reuses the suite runner (e.g. a TODO-227-style remediation\n"
        "driver) -- never as a routine way to silence a real leak.\n"
        + "!" * 78 + "\n"
    )
    print(banner)
    warnings.warn(banner, stacklevel=3)


def _tripwire_setup():
    """Returns (enabled, before_snapshots). before_snapshots is None when
    disabled via the escape hatch (a loud warning is printed either way the
    escape hatch is exercised)."""
    if os.environ.get(SKIP_ENV_VAR) == "1":
        _print_skip_warning()
        return False, None
    roots = _tripwire_roots()
    before = {name: _snapshot_tree(root) for name, root in roots.items()}
    return True, before


def _tripwire_teardown(enabled: bool, before) -> dict:
    """Returns a dict of {tree_name: {"added": [...], "removed": [...],
    "changed": [...]}} for every tree that changed (empty dict if none did,
    or if the tripwire was disabled -- disabled is not a violation)."""
    if not enabled:
        return {}
    roots = _tripwire_roots()
    full_diff = {}
    for name, root in roots.items():
        after = _snapshot_tree(root)
        added, removed, changed = _diff_snapshots(before[name], after)
        if added or removed or changed:
            full_diff[name] = {"added": added, "removed": removed, "changed": changed}
    return full_diff


def _format_diff(diff: dict) -> str:
    lines = [
        "",
        "=" * 78,
        "TODO-320 PRODUCTION-TREE TRIPWIRE FIRED",
        "One or more production trees changed during this test run -- a test is",
        "writing outside its isolation. See TODO-227 Phase 2b (5fbcafa, 5c8cc9c)",
        "for the exact fix pattern (tmp-dir copy + monkeypatch pipeline.DEFAULT_",
        "PARSED_DIR) used on the last two leaks found this way.",
        "=" * 78,
    ]
    for tree_name, d in diff.items():
        lines.append(f"\n[{tree_name}]")
        for label in ("added", "removed", "changed"):
            paths = d.get(label) or []
            if paths:
                lines.append(f"  {label} ({len(paths)}):")
                for p in paths:
                    lines.append(f"    {p}")
    lines.append("=" * 78)
    return "\n".join(lines)


@pytest.fixture(scope="session", autouse=True)
def _production_tree_tripwire():
    enabled, before = _tripwire_setup()
    yield
    _tripwire_result["diff"] = _tripwire_teardown(enabled, before)


def pytest_sessionfinish(session, exitstatus):
    diff = _tripwire_result.get("diff")
    if not diff:
        return
    print(_format_diff(diff))
    session.exitstatus = 1
