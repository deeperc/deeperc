"""corpus_baseline.git_dirty() — must ignore untracked files.

A bare `git status --porcelain` counts untracked scratch output (e.g. a
demo run's corpus_results/) as "dirty", so a tracked tree that exactly
matches its own commit was misreported as dirty. Fixed via
--untracked-files=no. Verdict-inert -- git_dirty feeds provenance metadata
(baseline filenames, report "git_dirty" field) only, never a checker
verdict.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import corpus_baseline  # noqa: E402


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _commit_all(path, message="init"):
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)


def test_untracked_file_alone_is_not_dirty(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    _commit_all(tmp_path)

    (tmp_path / "scratch_output.json").write_text("{}\n")  # untracked, uncommitted

    assert corpus_baseline.git_dirty(cwd=tmp_path) is False


def test_modified_tracked_file_is_dirty(tmp_path):
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n")
    _commit_all(tmp_path)

    tracked.write_text("v2\n")  # real change to a tracked file

    assert corpus_baseline.git_dirty(cwd=tmp_path) is True


def test_clean_tree_with_untracked_file_present_is_not_dirty(tmp_path):
    """The exact regression case: a clean tracked tree plus scratch output
    (e.g. corpus_results/ from a demo run) must not read as dirty."""
    _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    _commit_all(tmp_path)

    scratch_dir = tmp_path / "corpus_results"
    scratch_dir.mkdir()
    (scratch_dir / "report.json").write_text("{}\n")

    assert corpus_baseline.git_dirty(cwd=tmp_path) is False


# ── TODO-489: baseline pointer files do not count as dirty ───────────────────
#
# A baseline save rewrites its tracked latest.json pointer, so a second save in
# the same cycle named its file '-dirty' on an otherwise clean tree. Only the
# pointer paths are excluded; every other tracked change still counts.

import pytest  # noqa: E402

_POINTERS = sorted(corpus_baseline.BASELINE_POINTER_PATHS)


def _repo_with_pointers(path):
    _init_repo(path)
    for rel in _POINTERS:
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n")
    (path / "tracked.txt").write_text("v1\n")
    _commit_all(path)


def test_pointer_paths_are_the_two_baseline_pointers():
    assert corpus_baseline.BASELINE_POINTER_PATHS == frozenset({
        "corpus_results/baselines/latest.json",
        "corpus_results/recall/baselines/latest.json",
    })


@pytest.mark.parametrize("pointer", _POINTERS)
def test_modified_pointer_alone_is_not_dirty(tmp_path, pointer):
    _repo_with_pointers(tmp_path)

    (tmp_path / pointer).write_text('{"rewritten": true}\n')

    assert corpus_baseline.git_dirty(cwd=tmp_path) is False


def test_both_pointers_modified_is_not_dirty(tmp_path):
    _repo_with_pointers(tmp_path)

    for rel in _POINTERS:
        (tmp_path / rel).write_text('{"rewritten": true}\n')

    assert corpus_baseline.git_dirty(cwd=tmp_path) is False


def test_modified_other_tracked_file_is_still_dirty(tmp_path):
    _repo_with_pointers(tmp_path)

    (tmp_path / "tracked.txt").write_text("v2\n")

    assert corpus_baseline.git_dirty(cwd=tmp_path) is True


@pytest.mark.parametrize("pointer", _POINTERS)
def test_pointer_plus_other_tracked_file_is_dirty(tmp_path, pointer):
    _repo_with_pointers(tmp_path)

    (tmp_path / pointer).write_text('{"rewritten": true}\n')
    (tmp_path / "tracked.txt").write_text("v2\n")

    assert corpus_baseline.git_dirty(cwd=tmp_path) is True


def test_pointer_plus_untracked_file_is_not_dirty(tmp_path):
    """Tracked-only scope preserved alongside the pointer exclusion."""
    _repo_with_pointers(tmp_path)

    (tmp_path / _POINTERS[0]).write_text('{"rewritten": true}\n')
    (tmp_path / "scratch_output.json").write_text("{}\n")  # untracked

    assert corpus_baseline.git_dirty(cwd=tmp_path) is False


def test_similarly_named_tracked_file_is_not_excluded(tmp_path):
    """Exclusion is by exact repo-relative path, not basename: a latest.json
    elsewhere in the tree is an ordinary tracked file."""
    _repo_with_pointers(tmp_path)
    other = tmp_path / "docs" / "latest.json"
    other.parent.mkdir()
    other.write_text("{}\n")
    _commit_all(tmp_path, "add docs/latest.json")

    other.write_text('{"changed": true}\n')

    assert corpus_baseline.git_dirty(cwd=tmp_path) is True
