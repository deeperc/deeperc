"""Unit tests for TODO-320's production-tree tripwire (tests/conftest.py).

Exercises the plain, pytest-fixture-independent functions directly against
isolated tmp trees (monkeypatching pipeline.DEFAULT_PARSED_DIR /
DEFAULT_DATASHEETS_DIR, never touching real production dirs) -- this proves
the detection logic itself, decoupled from pytest's own fixture-teardown
timing.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
POC = REPO / "schematic_checker_poc"
sys.path.insert(0, str(POC))
sys.path.insert(0, str(REPO))

import pipeline  # noqa: E402
from tests import conftest as tripwire  # noqa: E402


@pytest.fixture
def isolated_trees(tmp_path, monkeypatch):
    """Two empty tmp dirs standing in for datasheets_parsed/ and
    netlist_corpus/datasheets/, wired via the same pipeline constants the
    real tripwire reads."""
    parsed_dir = tmp_path / "datasheets_parsed"
    corpus_dir = tmp_path / "netlist_corpus_datasheets"
    parsed_dir.mkdir()
    corpus_dir.mkdir()
    monkeypatch.setattr(pipeline, "DEFAULT_PARSED_DIR", str(parsed_dir))
    monkeypatch.setattr(pipeline, "DEFAULT_DATASHEETS_DIR", str(corpus_dir))
    return parsed_dir, corpus_dir


def test_clean_pass_through(isolated_trees, monkeypatch):
    parsed_dir, corpus_dir = isolated_trees
    (parsed_dir / "PARTA").mkdir()
    (parsed_dir / "PARTA" / "PARTA.md").write_text("unchanged")

    enabled, before = tripwire._tripwire_setup()
    assert enabled is True

    diff = tripwire._tripwire_teardown(enabled, before)
    assert diff == {}


def test_detects_added_file(isolated_trees):
    parsed_dir, corpus_dir = isolated_trees
    (parsed_dir / "PARTA").mkdir()
    (parsed_dir / "PARTA" / "PARTA.md").write_text("original")

    enabled, before = tripwire._tripwire_setup()

    (parsed_dir / "PARTA" / "patch_state.json").write_text("{}")

    diff = tripwire._tripwire_teardown(enabled, before)
    assert "datasheets_parsed" in diff
    assert diff["datasheets_parsed"]["added"] == [
        str(Path("PARTA") / "patch_state.json")
    ]
    assert diff["datasheets_parsed"]["removed"] == []
    assert diff["datasheets_parsed"]["changed"] == []


def test_detects_modified_file(isolated_trees):
    parsed_dir, corpus_dir = isolated_trees
    target = parsed_dir / "PARTA"
    target.mkdir()
    md = target / "PARTA.md"
    md.write_text("original content")

    enabled, before = tripwire._tripwire_setup()

    # Force a real (size AND mtime) change -- different length is sufficient
    # on its own, but this also exercises mtime_ns changing.
    md.write_text("mutated, longer content than before")

    diff = tripwire._tripwire_teardown(enabled, before)
    assert diff["datasheets_parsed"]["changed"] == [str(Path("PARTA") / "PARTA.md")]
    assert diff["datasheets_parsed"]["added"] == []
    assert diff["datasheets_parsed"]["removed"] == []


def test_detects_removed_file(isolated_trees):
    parsed_dir, corpus_dir = isolated_trees
    target = parsed_dir / "PARTA"
    target.mkdir()
    md = target / "PARTA.md"
    md.write_text("original")

    enabled, before = tripwire._tripwire_setup()

    md.unlink()

    diff = tripwire._tripwire_teardown(enabled, before)
    assert diff["datasheets_parsed"]["removed"] == [str(Path("PARTA") / "PARTA.md")]
    assert diff["datasheets_parsed"]["added"] == []
    assert diff["datasheets_parsed"]["changed"] == []


def test_detects_change_in_second_tree(isolated_trees):
    parsed_dir, corpus_dir = isolated_trees
    (corpus_dir / "misc").mkdir()
    (corpus_dir / "misc" / "some_datasheet.pdf").write_text("pdf bytes")

    enabled, before = tripwire._tripwire_setup()

    (corpus_dir / "misc" / "new_datasheet.pdf").write_text("more pdf bytes")

    diff = tripwire._tripwire_teardown(enabled, before)
    assert "netlist_corpus_datasheets" in diff
    assert "datasheets_parsed" not in diff
    assert diff["netlist_corpus_datasheets"]["added"] == [
        str(Path("misc") / "new_datasheet.pdf")
    ]


def test_escape_hatch_disables_and_warns(isolated_trees, monkeypatch):
    parsed_dir, corpus_dir = isolated_trees
    monkeypatch.setenv(tripwire.SKIP_ENV_VAR, "1")

    with pytest.warns(UserWarning, match="TODO-320"):
        enabled, before = tripwire._tripwire_setup()
    assert enabled is False
    assert before is None

    (parsed_dir / "sneaky_write.txt").write_text("should not be flagged")

    diff = tripwire._tripwire_teardown(enabled, before)
    assert diff == {}


def test_missing_tree_snapshots_as_empty(tmp_path):
    missing = tmp_path / "does_not_exist"
    assert tripwire._snapshot_tree(missing) == {}


def test_diff_snapshots_helper_directly():
    before = {"a.md": (10, 100), "b.md": (20, 200)}
    after = {"a.md": (10, 100), "b.md": (25, 200), "c.md": (5, 50)}
    added, removed, changed = tripwire._diff_snapshots(before, after)
    assert added == ["c.md"]
    assert removed == []
    assert changed == ["b.md"]
